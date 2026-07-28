"""Frozen selected-product route on the exact Lakeline #659 guide profile."""
import json

import pytest
from fastapi.testclient import TestClient

from app import app, load_store
from router import engine


client = TestClient(app)
STORE = load_store("659")


@pytest.fixture
def clean_located_products():
    app.state.catalog_cache.clear_cache("located")
    yield
    app.state.catalog_cache.clear_cache("located")


def test_real_659_product_route_matches_the_exact_map_golden(
        monkeypatch, clean_located_products):
    golden = json.load(open("data/659-atlas/golden_route.json"))

    class FakeHEB:
        async def locate(self, product_id, location_label, atlas, store=None):
            return {
                "point": atlas["geometry"]["anchors"]["PRODUCE"],
                "group": "ANCHOR:PRODUCE",
                "approx": True,
                "location_label": location_label,
            }

    monkeypatch.setattr(app.state, "heb", FakeHEB(), raising=False)
    assert client.post("/api/products/locate", json={
        "products": golden["products"],
    }).status_code == 200

    response = client.post("/api/route", json={"items": [
        {"product_id": product["id"], "quantity": 2 if index == 0 else 1}
        for index, product in enumerate(golden["products"])
    ]})
    body = response.json()

    assert response.status_code == 200
    assert [{key: stop[key] for key in ("n", "product_id", "quantity")}
            for stop in body["stops"]] == golden["stops"]
    assert body["distance_m"] == golden["distance_m"]
    assert body["baseline_m"] == golden["baseline_m"]
    assert body["saved_m"] == golden["saved_m"]
    assert body["path"] == golden["path"]
    assert engine.path_is_legal(
        STORE.free, body["path"], STORE.cell) == []
