from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

LIST_25 = ["milk", "bananas", "shrimp", "sourdough bread", "sliced turkey",
           "tortilla chips", "pasta", "ice cream", "paper towels", "toothpaste",
           "dog food", "shampoo", "cereal", "coffee", "olive oil", "rice",
           "frozen pizza", "yogurt", "eggs", "butter", "salsa", "peanut butter",
           "dish soap", "trash bags", "batteries"]

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
    located = sum(len(s["items"]) for s in body["stops"])
    assert located / 25 >= 0.85                       # G2: >=85% auto-located
    assert located + len(body["unmatched"]) == 25     # never dropped

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
