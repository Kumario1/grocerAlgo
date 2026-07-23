from pathlib import Path

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

from route_demo import ACCEPTANCE_LIST as LIST_25   # one canonical acceptance list

def test_geometry_endpoint():
    g = client.get("/api/geometry").json()
    aisles = [name for name in g["anchors"] if name.startswith("AISLE ")]

    assert g["page"] == {"w": 1266.0, "h": 834.0}
    assert len(aisles) == 45


def test_walkability_endpoint_returns_the_exact_659_qa_overlay():
    response = client.get("/api/walkability.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == Path(
        "data/659/qa/walkable_overlay.png").read_bytes()


def test_route_small_list():
    r = client.post("/api/route", json={"items": ["milk", "dog food", "bread"]})
    assert r.status_code == 200
    body = r.json()
    assert body["stops"][0]["n"] == 1
    assert body["distance_m"] > 0
    assert len(body["path"]) > 10                     # a real polyline

def test_route_25_items_fast_and_covered():
    import time
    t0 = time.time()
    body = client.post("/api/route", json={"items": LIST_25}).json()
    assert time.time() - t0 < 1.0                     # G3
    located = len(body["stops"])                      # one stop per item now
    assert located / 25 >= 0.85                       # G2: >=85% auto-located
    assert located + len(body["unmatched"]) == 25     # never dropped

def test_two_items_in_one_aisle_stay_two_stops():
    """Consolidation is a solver optimisation, not a display rule: you walk into
    aisle 8 once, but coffee and cereal are two products in two places and must
    be shown, and numbered, separately."""
    body = client.post("/api/route", json={"items": ["coffee", "cereal"]}).json()
    a, b = body["stops"]
    assert a["anchor"] == b["anchor"]                 # same aisle
    assert [a["n"], b["n"]] == [1, 2]                 # still numbered in walk order
    assert (a["x"], a["y"]) != (b["x"], b["y"])       # at their own shelf spots
    assert not a["approx"] and not b["approx"]

def test_aisle_direction_uses_the_better_end():
    """The route visits products in either segment direction; it is not forced
    back to the numbered aisle mouth after collecting them."""
    body = client.post("/api/route", json={"items": ["coffee", "cereal"]}).json()
    path = [tuple(p) for p in body["path"]]
    edges = list(zip(path, path[1:]))
    assert not any((b, a) in edges for a, b in edges)

def test_savings_telemetry_is_measured_not_guessed():
    """G1. Baseline and route are both measured off assembled paths, so the
    numbers are comparable rather than two different metrics."""
    body = client.post("/api/route", json={"items": LIST_25}).json()
    assert body["baseline_m"] > body["distance_m"] > 0
    assert round(body["baseline_m"] - body["distance_m"], 1) == body["saved_m"]

def test_unmatched_flagged_not_dropped():
    body = client.post("/api/route", json={"items": ["milk", "flux capacitor"]}).json()
    assert body["unmatched"][0]["query"] == "flux capacitor"
    assert body["unmatched"][0]["suggestions"]

def test_empty_list_400():
    assert client.post("/api/route", json={"items": []}).status_code == 400

def test_route_stays_inside_boundary():
    import json
    from router import engine
    geom = json.load(open("data/659/geometry.json"))
    bmask = engine.build_grid({"page": geom["page"],
                               "boundary": geom["boundary"],
                               "fixtures": [], "obstacle_paths": []})
    body = client.post("/api/route", json={"items": LIST_25}).json()
    outside = [(x, y) for x, y in body["path"]
               if not bmask[int(y // engine.CELL), int(x // engine.CELL)]]
    assert not outside, f"route leaves the store at {outside[:5]}"


def test_product_search_returns_real_heb_suggestions(monkeypatch):
    product = {
        "id": "1657904",
        "name": "Fresh Sweet Cob Corn, 8 ct",
        "brand": "Fresh",
        "size": "8 ct",
        "image_url": "https://img/corn.jpg",
        "inventory_state": "IN_STOCK",
        "location_label": "In Produce on the Front Wall",
        "selectable": True,
    }

    class FakeHEB:
        async def search(self, query):
            return [product]

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)

    response = client.get("/api/products", params={"q": "milk"})

    assert response.status_code == 200
    assert response.json() == {"products": [product]}


def test_product_search_requires_three_non_whitespace_characters():
    assert client.get("/api/products", params={"q": "mi"}).status_code == 422
    assert client.get("/api/products", params={"q": "  a"}).status_code == 422


def test_product_search_keeps_out_of_stock_results_disabled(monkeypatch):
    out_of_stock = {
        "id": "100",
        "name": "H-E-B Whole Milk",
        "brand": "H-E-B",
        "size": "1 gal",
        "image_url": None,
        "inventory_state": "OUT_OF_STOCK",
        "location_label": "In Dairy",
        "selectable": False,
    }

    class FakeHEB:
        async def search(self, query):
            return [out_of_stock]

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)

    product = client.get("/api/products", params={"q": "milk"}).json()[
        "products"][0]
    assert product["inventory_state"] == "OUT_OF_STOCK"
    assert product["selectable"] is False


def test_heb_connection_flow_reports_status(monkeypatch):
    class FakeHEB:
        connected = False

        def status(self):
            return {"connected": self.connected, "map_ready": self.connected,
                    "store_id": 659}

        async def connect(self):
            return self.status()

        async def confirm(self):
            self.connected = True
            return self.status()

    fake = FakeHEB()
    monkeypatch.setattr(app.state, "heb", fake, raising=False)

    assert client.get("/api/heb/status").json()["connected"] is False
    assert client.post("/api/heb/connect").status_code == 200
    assert client.post("/api/heb/connect/confirm").json() == {
        "connected": True, "map_ready": True, "store_id": 659,
    }


def test_locate_products_returns_reachable_atlas_placements(monkeypatch):
    class FakeHEB:
        async def locate(self, product_id, location_label, atlas):
            return {
                "point": atlas["geometry"]["anchors"]["PRODUCE"],
                "group": "ANCHOR:PRODUCE",
                "approx": True,
                "location_label": location_label,
            }

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)
    response = client.post("/api/products/locate", json={"products": [{
        "id": "1657904",
        "name": "Fresh Sweet Cob Corn, 8 ct",
        "brand": "Fresh",
        "size": "8 ct",
        "image_url": "https://img/corn.jpg",
        "inventory_state": "IN_STOCK",
        "location_label": "In Produce on the Front Wall",
        "selectable": True,
    }]})

    assert response.status_code == 200
    product = response.json()["products"][0]
    assert product["id"] == "1657904"
    assert product["routable"] is True
    assert product["approx"] is True
    assert product["location_label"] == "In Produce on the Front Wall"
    rehydrated = client.post(
        "/api/products/locate",
        json={"products": [{
            "id": "1657904",
            "name": "Fresh Sweet Cob Corn, 8 ct",
            "brand": "Fresh",
            "size": "8 ct",
            "image_url": "https://img/corn.jpg",
            "inventory_state": "IN_STOCK",
            "location_label": "In Produce on the Front Wall",
            "selectable": True,
        }]},
    ).json()["products"][0]
    assert rehydrated["routable"] is True


def test_locate_products_preserves_exact_pals_section_on_the_guide(monkeypatch):
    class FakeHEB:
        async def locate(self, product_id, location_label, atlas):
            return {
                "point": [225.5742, 177.3972],
                "group": "PSA:01:13",
                "approx": False,
                "location_label": "Aisle 13",
            }

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)
    response = client.post("/api/products/locate", json={"products": [{
        "id": "2102898",
        "name": "Aisle 13 product",
        "location_label": "Aisle 13",
    }]})

    assert response.status_code == 200
    product = response.json()["products"][0]
    from app import CELL, FREE, GEOM
    assert [product["x"], product["y"]] == [485.0, 141.0]
    assert [product["x"], product["y"]] != GEOM["anchors"]["AISLE 13"]
    assert product["x"] % CELL == CELL / 2
    assert product["y"] % CELL == CELL / 2
    assert FREE[int(product["y"] // CELL), int(product["x"] // CELL)]


def test_department_edge_pin_is_the_reachable_route_stop(monkeypatch):
    class FakeHEB:
        async def locate(self, product_id, location_label, atlas):
            return {
                "point": [625.05, 410.1768],
                "group": "PSA:05:86",
                "approx": False,
                "location_label": "On the Left Edge of Bakery",
            }

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)
    product = {
        "id": "2118487",
        "name": "Cake",
        "location_label": "On the Left Edge of Bakery",
    }
    located = client.post(
        "/api/products/locate", json={"products": [product]}
    ).json()["products"][0]
    route = client.post("/api/route", json={"items": [{
        "product_id": "2118487",
        "quantity": 1,
    }]}).json()

    from app import CELL, FREE, GEOM
    assert [located["x"], located["y"]] != GEOM["anchors"]["BAKERY"]
    assert FREE[int(located["y"] // CELL), int(located["x"] // CELL)]
    assert [located["x"], located["y"]] in route["path"]
    assert [route["stops"][0]["x"], route["stops"][0]["y"]] in route["path"]


def test_selected_products_route_consolidates_quantity_and_reports_unrouted(
        monkeypatch):
    placed = {
        "milk": ("DAIRY", "Aisle 41"),
        "cheese": ("DAIRY", "In Dairy"),
        "bread": ("BAKERY", "In Bakery"),
    }

    class FakeHEB:
        async def locate(self, product_id, location_label, atlas):
            if product_id == "mystery":
                return None
            anchor, label = placed[product_id]
            return {
                "point": atlas["geometry"]["anchors"][anchor],
                "group": f"ANCHOR:{anchor}",
                "approx": True,
                "location_label": label,
            }

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)
    products = [
        {"id": product_id, "name": product_id.title(), "brand": "H-E-B",
         "size": "1 ct", "image_url": None, "inventory_state": "IN_STOCK",
         "location_label": label, "selectable": True}
        for product_id, (_, label) in placed.items()
    ] + [{
        "id": "mystery", "name": "Mystery", "brand": "H-E-B", "size": "1 ct",
        "image_url": None, "inventory_state": "IN_STOCK",
        "location_label": None, "selectable": True,
    }]
    located = client.post(
        "/api/products/locate", json={"products": products})
    assert located.status_code == 200
    milk = next(product for product in located.json()["products"]
                if product["id"] == "milk")
    from app import CELL, FREE, GEOM
    dairy = GEOM["anchors"]["DAIRY"]
    assert abs(milk["x"] - dairy[0]) <= CELL
    assert abs(milk["y"] - dairy[1]) <= CELL
    assert FREE[int(milk["y"] // CELL), int(milk["x"] // CELL)]

    response = client.post("/api/route", json={"items": [
        {"product_id": "milk", "quantity": 1},
        {"product_id": "milk", "quantity": 2},
        {"product_id": "cheese", "quantity": 1},
        {"product_id": "bread", "quantity": 1},
        {"product_id": "mystery", "quantity": 1},
    ]})

    assert response.status_code == 200
    body = response.json()
    quantities = {stop["product_id"]: stop["quantity"]
                  for stop in body["stops"]}
    assert quantities == {"milk": 3, "cheese": 1, "bread": 1}
    dairy_groups = {stop["placement_group"] for stop in body["stops"]
                    if stop["product_id"] in {"milk", "cheese"}}
    assert dairy_groups == {"ANCHOR:DAIRY"}
    assert body["unrouted"][0]["product_id"] == "mystery"
    assert body["distance_m"] > 0
    assert len(body["path"]) > 2


def test_selected_route_is_422_when_nothing_is_routable(monkeypatch):
    class FakeHEB:
        async def locate(self, product_id, location_label, atlas):
            return None

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)
    product = {
        "id": "mystery", "name": "Mystery", "brand": None, "size": None,
        "image_url": None, "inventory_state": "IN_STOCK",
        "location_label": None, "selectable": True,
    }
    assert client.post("/api/products/locate",
                       json={"products": [product]}).status_code == 200
    response = client.post("/api/route", json={
        "items": [{"product_id": "mystery", "quantity": 1}],
    })
    assert response.status_code == 422
    assert response.json()["detail"] == "no selected product is routable"
