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
from app import LOCATED_PRODUCTS, app, load_store

client = TestClient(app)
STORE = load_store("659")


@pytest.fixture(autouse=True)
def clean():
    LOCATED_PRODUCTS.clear()
    yield
    LOCATED_PRODUCTS.clear()


def locate(placement):
    class FakeHEB:
        async def locate(self, product_id, location_label, atlas):
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
