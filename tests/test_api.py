from pathlib import Path

from fastapi.testclient import TestClient
from app import app, load_store
from router.heb import HEBBusyError

client = TestClient(app)
STORE = load_store("659")

from route_demo import ACCEPTANCE_LIST as LIST_25   # one canonical acceptance list

def test_geometry_endpoint():
    g = client.get("/api/geometry").json()
    aisles = [name for name in g["anchors"] if name.startswith("AISLE ")]

    assert g["page"] == {"w": 1266.0, "h": 834.0}
    assert len(aisles) == 45


def test_public_page_has_no_browser_bootstrap_controls():
    html = client.get("/").text

    assert 'id="connect"' not in html
    assert 'id="confirm"' not in html


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
        async def search(self, query, store=None):
            return [product]

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)

    response = client.get("/api/products", params={"q": "milk"})

    assert response.status_code == 200
    assert response.json() == {"products": [product]}


def test_product_search_requires_three_non_whitespace_characters():
    assert client.get("/api/products", params={"q": "mi"}).status_code == 422
    assert client.get("/api/products", params={"q": "  a"}).status_code == 422


def test_product_search_reports_browser_queue_back_pressure(monkeypatch):
    class BusyHEB:
        async def search(self, query, store=None):
            raise HEBBusyError(5)

    monkeypatch.setattr(app.state, "heb", BusyHEB(), raising=False)

    response = client.get("/api/products", params={"q": "milk"})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"


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
        async def search(self, query, store=None):
            return [out_of_stock]

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)

    product = client.get("/api/products", params={"q": "milk"}).json()[
        "products"][0]
    assert product["inventory_state"] == "OUT_OF_STOCK"
    assert product["selectable"] is False


def test_heb_connection_flow_reports_status(monkeypatch):
    class FakeHEB:
        connected = False

        def status(self, store=None):
            return {"connected": self.connected, "map_ready": self.connected,
                    "store_id": 659}

        async def connect(self, store=None, fresh=True):
            return self.status()

        async def confirm(self, store=None):
            self.connected = True
            return self.status()

    fake = FakeHEB()
    monkeypatch.setattr(app.state, "heb", fake, raising=False)
    monkeypatch.setenv("GROCER_ADMIN_TOKEN", "s3cret")

    assert client.get("/api/heb/status").json()["connected"] is False
    assert client.post("/api/heb/connect").status_code == 401
    headers = {"Authorization": "Bearer s3cret"}
    assert client.post("/api/heb/connect", headers=headers).status_code == 200
    assert client.post(
        "/api/heb/connect/confirm", headers=headers).json() == {
        "connected": True, "map_ready": True, "store_id": 659,
    }


def test_heb_state_transfer_is_admin_only(monkeypatch):
    state = {"cookies": [{"name": "store", "value": "659",
                          "domain": ".heb.com", "path": "/"}],
             "origins": []}

    class FakeHEB:
        async def import_state(self, store, supplied):
            assert int(store) == 659
            assert supplied == state
            return {"connected": True, "map_ready": True, "store_id": 659}

        def export_state(self, store):
            assert int(store) == 659
            return state

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)
    monkeypatch.setenv("GROCER_ADMIN_TOKEN", "s3cret")
    headers = {"Authorization": "Bearer s3cret"}

    assert client.put("/api/heb/state", json=state).status_code == 401
    assert client.put(
        "/api/heb/state", json=state, headers=headers).json()["map_ready"] is True
    assert client.get("/api/heb/state", headers=headers).json() == state


def test_heb_recovery_status_is_admin_only(monkeypatch):
    class FakeHEB:
        def recovery_status(self, store):
            return {
                "connected": True,
                "map_ready": True,
                "store_id": int(store),
                "cache": {"search": 20, "placement": 5, "located": 5},
            }

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)
    monkeypatch.setenv("GROCER_ADMIN_TOKEN", "s3cret")

    assert client.get("/api/heb/recovery").status_code == 401
    response = client.get(
        "/api/heb/recovery",
        headers={"Authorization": "Bearer s3cret"})
    assert response.json()["cache"]["placement"] == 5


def test_locate_products_returns_reachable_atlas_placements(monkeypatch):
    class FakeHEB:
        async def locate(self, product_id, location_label, atlas, store=None):
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
        async def locate(self, product_id, location_label, atlas, store=None):
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
    store = STORE
    # On aisle 13's corridor, and half way down it — the PALS section is
    # mid-aisle, not at the mouth where the aisle number is printed.
    assert [product["x"], product["y"]] == [491.0, 239.0]
    assert abs(product["x"] - store.geometry["anchors"]["AISLE 13"][0]) < store.cell
    assert product["y"] > store.geometry["anchors"]["AISLE 13"][1] + 50
    assert product["x"] % store.cell == store.cell / 2
    assert product["y"] % store.cell == store.cell / 2
    assert store.free[int(product["y"] // store.cell), int(product["x"] // store.cell)]


def test_department_edge_pin_is_the_reachable_route_stop(monkeypatch):
    class FakeHEB:
        async def locate(self, product_id, location_label, atlas, store=None):
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

    store = STORE
    assert [located["x"], located["y"]] != store.geometry["anchors"]["BAKERY"]
    assert store.free[int(located["y"] // store.cell), int(located["x"] // store.cell)]
    assert [located["x"], located["y"]] in route["path"]
    assert [route["stops"][0]["x"], route["stops"][0]["y"]] in route["path"]


def test_selected_products_route_consolidates_quantity_and_reports_unrouted(
        monkeypatch):
    # resolve_placement only falls back to a department ANCHOR when the label
    # names no aisle, so these labels are department-only on purpose.
    placed = {
        "milk": ("DAIRY", "In Dairy on the Back Wall"),
        "cheese": ("DAIRY", "In Dairy"),
        "bread": ("BAKERY", "In Bakery"),
    }

    class FakeHEB:
        async def locate(self, product_id, location_label, atlas, store=None):
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
    store = STORE
    dairy = store.geometry["anchors"]["DAIRY"]
    assert abs(milk["x"] - dairy[0]) <= store.cell
    assert abs(milk["y"] - dairy[1]) <= store.cell
    assert store.free[int(milk["y"] // store.cell), int(milk["x"] // store.cell)]

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
        async def locate(self, product_id, location_label, atlas, store=None):
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


def test_transform_does_not_bend_a_shelf_face():
    """Mapping a straight shelf run must not make it wider than it is.

    The rubber-sheet transform this guards against was fitted through label
    positions lying on three near-collinear lines, so it stretched aisle 17's
    35 pt-wide shelf run across 93 pt of guide — nearly three aisles — and
    products pinned beside the aisle they were really in.
    """
    import collections

    import app

    faces = collections.defaultdict(list)
    for key, point in STORE.atlas["psas"].items():
        area, _, side, _ = key.split("|")
        faces[(area, key.split("|")[1], side)].append(point)

    def thin_spread(points):
        axis = min((0, 1), key=lambda a: max(p[a] for p in points)
                   - min(p[a] for p in points))
        return axis, (max(p[axis] for p in points)
                      - min(p[axis] for p in points))

    checked = 0
    for points in faces.values():
        if len(points) < 6:
            continue
        axis, before = thin_spread(points)
        mapped = [app.atlas_to_guide(STORE, p) for p in points]
        after = (max(p[axis] for p in mapped) - min(p[axis] for p in mapped))
        assert after <= before * 1.05 + 1, (
            f"a {before:.0f} pt shelf run became {after:.0f} pt of guide")
        checked += 1
    assert checked > 100


def test_aisle_products_land_on_the_aisle_the_label_names():
    import app

    ice_cream = app.exact_map_point(STORE, "Aisle 17", {
        "point": STORE.atlas["psas"]["04|17|A|12"],
        "psa_key": "04|17|A|12",
        "group": "PSA:04:17",
    })
    aisle = STORE.geometry["anchors"]["AISLE 17"]
    # On aisle 17's corridor — the neighbouring aisles are 39 and 69 pt away.
    assert abs(ice_cream[0] - aisle[0]) < 5
    # ...and down the aisle, not parked at its mouth.
    assert ice_cream[1] > aisle[1]


def test_off_floor_pallet_slot_defers_to_the_printed_aisle():
    """H-E-B answers for some bulk packs with a slot off the shopping floor.

    16|88 is a pallet bay in the bottom-left vestibule that H-E-B itself
    labels "Aisle 13". Trusting the point parked a 40-pack of water at the
    store entrance, 32 m from the aisle the shopper was told to walk to.
    """
    import app

    stray = app.exact_map_point(STORE, "Aisle 13", {
        "point": STORE.atlas["psas"]["16|88|A|4"],
        "psa_key": "16|88|A|4",
        "group": "PSA:16:88",
    })
    assert stray == STORE.geometry["anchors"]["AISLE 13"]
    assert app.snap_distance_m(STORE, stray) <= app.MAX_SNAP_M


def test_no_psa_can_strand_a_product_off_the_shopping_floor():
    """Every Atlas PSA either lands on the floor or defers to its label."""
    import app

    stranded = []
    for key, point in STORE.atlas["psas"].items():
        group = "PSA:" + ":".join(key.split("|")[:2])
        mapped = app.exact_map_point(STORE, None, {"point": point, "group": group})
        if app.snap_distance_m(STORE, mapped) > app.MAX_SNAP_M:
            stranded.append(key)
    # Without a usable label there is nothing better to fall back to, so a
    # handful of pallet bays stay off-floor — but they must stay a handful.
    assert len(stranded) <= 16, stranded
    for key in stranded:
        labelled = app.exact_map_point(STORE, "Aisle 13", {
            "point": STORE.atlas["psas"][key],
            "group": "PSA:" + ":".join(key.split("|")[:2])})
        assert app.snap_distance_m(STORE, labelled) <= app.MAX_SNAP_M
