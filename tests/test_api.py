from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

from route_demo import ACCEPTANCE_LIST as LIST_25   # one canonical acceptance list

def test_geometry_endpoint():
    g = client.get("/api/geometry").json()
    assert g["page"]["w"] > 0 and len(g["fixtures"]) > 100

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
