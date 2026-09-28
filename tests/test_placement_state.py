"""Exact is earned, not asserted.

A product is called exact only when H-E-B gave shelf-face geometry for it, the
store's calibration passed, and the mapped point lands on floor a shopper can
stand on. Everything else is a department-level fact and has to look like one —
the app used to stamp every placement approximate, which hid the difference in
the other direction.
"""
import pytest
from fastapi.testclient import TestClient

import app as application
from app import app, load_store

client = TestClient(app)
STORE = load_store("659")


@pytest.fixture(autouse=True)
def clean():
    app.state.catalog_cache.clear_cache("located")
    yield
    app.state.catalog_cache.clear_cache("located")


def locate(placement):
    class FakeHEB:
        async def locate(self, product_id, location_label, atlas, store=None):
            return placement

    original = getattr(app.state, "heb")
    app.state.heb = FakeHEB()
    try:
        return client.post("/api/products/locate", json={"products": [{
            "id": "1", "name": "Thing",
            "location_label": placement.get("location_label"),
        }]}).json()["products"][0]
    finally:
        app.state.heb = original


def test_a_shelf_face_placement_is_exact():
    product = locate({
        "point": STORE.atlas["psas"]["04|17|A|12"],
        "psa_key": "04|17|A|12",
        "group": "PSA:04:17",
        "location_label": "Aisle 17",
    })

    assert product["placement_state"] == "exact"
    assert product["approx"] is False
    aisle = STORE.geometry["anchors"]["AISLE 17"]
    assert abs(product["x"] - aisle[0]) < 5      # on aisle 17's own corridor


def test_a_department_placement_says_department():
    product = locate({
        "point": STORE.atlas["geometry"]["anchors"]["PRODUCE"],
        "group": "ANCHOR:PRODUCE",
        "location_label": "In Produce on the Front Wall",
    })

    # H-E-B gave no shelf coordinate here — no calibration can invent one.
    assert product["placement_state"] == "department"
    assert product["approx"] is True


def test_an_off_floor_pallet_slot_is_not_passed_off_as_exact():
    product = locate({
        "point": STORE.atlas["psas"]["16|88|A|4"],
        "psa_key": "16|88|A|4",
        "group": "PSA:16:88",
        "location_label": "Aisle 13",
    })

    # The PSA is real but sits in a vestibule off the shopping floor, so the
    # printed label wins — and the result is department-level, not exact.
    assert product["placement_state"] == "department"
    assert product["approx"] is True
    assert application.snap_distance_m(
        STORE, [product["x"], product["y"]]) <= application.MAX_SNAP_M


def test_the_route_carries_the_placement_state_through_to_its_stops():
    locate({
        "point": STORE.atlas["psas"]["04|17|A|12"],
        "psa_key": "04|17|A|12",
        "group": "PSA:04:17",
        "location_label": "Aisle 17",
    })

    body = client.post("/api/route", json={
        "items": [{"product_id": "1", "quantity": 1}]}).json()

    assert body["stops"][0]["approximation_state"] == "exact"
    assert body["stops"][0]["approx"] is False


def test_heb_approximate_location_never_becomes_exact():
    from router.heb import resolve_placement

    placement = resolve_placement({"results": [{"approximateLocation": {
        "area": "04", "aisle": "17", "side": "A", "section": "12",
    }}]}, STORE.atlas, "Aisle 17")
    product = locate(placement)

    assert product["placement_state"] == "department"
    assert product["placement_group"] == "ANCHOR:AISLE 17"


def test_conflicting_aisle_falls_back_to_the_label_on_811(monkeypatch):
    from router.heb import resolve_placement

    store = load_store("811")
    placement = resolve_placement({"results": [{"psas": [{
        "area": "01", "aisle": "5", "side": "A", "section": "1",
    }]}]}, store.atlas, "Aisle 4")

    class FakeHEB:
        async def locate(self, *args):
            return placement

    monkeypatch.setattr(app.state, "heb", FakeHEB())
    response = client.post("/api/products/locate?store=811", json={
        "products": [{"id": "conflict", "name": "Conflicting shelf",
                      "location_label": "Aisle 4"}]})
    product = response.json()["products"][0]
    assert product["placement_state"] == "department"
    assert product["placement_group"] == "ANCHOR:AISLE 4"
    assert abs(product["x"] - store.geometry["anchors"]["AISLE 4"][0]) < 5
    route = client.post("/api/route?store=811", json={
        "items": [{"product_id": "conflict"}]}).json()
    assert route["stops"][0]["placement_group"] == "ANCHOR:AISLE 4"


def test_off_floor_without_a_label_is_unroutable():
    product = locate({
        "point": STORE.atlas["psas"]["16|88|A|4"],
        "psa_key": "16|88|A|4", "group": "PSA:16:88",
    })

    assert product["routable"] is False
    assert "x" not in product


def test_deep_shelf_in_811_keeps_its_own_aisle():
    store = load_store("811")
    point, state = application.place(store, "Aisle 4", {
        "point": store.atlas["psas"]["01|4|A|1"], "group": "PSA:01:4"})

    assert state == "exact"
    assert abs(point[0] - store.geometry["anchors"]["AISLE 4"][0]) < 5
    assert point[1] > 350  # retain the shelf's depth, not the aisle-mouth badge


def test_stale_located_cache_cannot_bypass_new_placement_checks():
    app.state.catalog_cache.save_cache("located", "659", "old", {
        "name": "Old placement", "routable": True, "route_cell": STORE.start,
        "placement_group": "PSA:01:5", "x": 1, "y": 1,
        "approx": False, "placement_state": "exact",
    }, 60)

    response = client.post("/api/route", json={
        "items": [{"product_id": "old"}]})
    assert response.status_code == 422


def test_a_blocked_calibration_cannot_route_previously_located_products(monkeypatch):
    locate({"point": STORE.atlas["psas"]["04|17|A|12"],
            "group": "PSA:04:17", "location_label": "Aisle 17"})
    monkeypatch.setattr(application.cal, "is_catalog_enabled", lambda store: False)
    response = client.post("/api/route", json={"items": [{"product_id": "1"}]})
    assert response.status_code == 409


def test_recalibration_invalidates_cached_map_and_product_cells(monkeypatch):
    import copy

    locate({"point": STORE.atlas["psas"]["04|17|A|12"],
            "group": "PSA:04:17", "location_label": "Aisle 17"})
    changed = copy.deepcopy(STORE.calibration)
    changed["x"]["offset"] += 1
    original = application.cal.load_calibration
    monkeypatch.setattr(application.cal, "load_calibration", lambda store:
                        changed if str(store) == "659" else original(store))
    try:
        refreshed = application.catalog_store("659")
        assert refreshed.calibration == changed
        assert refreshed is not STORE
        response = client.post("/api/route", json={"items": [{"product_id": "1"}]})
        assert response.status_code == 422  # must locate against the new transform
    finally:
        application.load_store.cache_clear()
