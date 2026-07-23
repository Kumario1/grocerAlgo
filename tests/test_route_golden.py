"""Frozen route for the 25-item acceptance list on #659 (plan.md Phase 2 exit).

The walkability golden (test_golden.py) freezes the FLOOR. This freezes the
ANSWER: which stops, in which order, at which coordinates, along which drawn
line. Between them, a change that alters where the router sends a shopper
cannot land silently.

Re-blessing is deliberate, like the pixel golden — there is no UPDATE_GOLDEN
env var on purpose. When a change is intended to move the route:

  1. make the change and get every other test green;
  2. python3 build_profile.py 659
  3. python3 -c "import json; from fastapi.testclient import TestClient; \
     from app import app; import route_demo as rd; \
     b=TestClient(app).post('/api/route',json={'items':rd.ACCEPTANCE_LIST}).json(); \
     g=json.load(open('data/659/golden_route.json')); \
     g.update(distance_m=b['distance_m'], baseline_m=b['baseline_m'], \
     saved_m=b['saved_m'], \
     stops=[{'n':s['n'],'item':s['item'],'anchor':s['anchor'], \
     'x':round(s['x'],2),'y':round(s['y'],2),'approx':s['approx']} for s in b['stops']], \
     path=[[round(x,2),round(y,2)] for x,y in b['path']], \
     unmatched=[u['query'] for u in b['unmatched']]); \
     json.dump(g, open('data/659/golden_route.json','w'), indent=1)"
  4. eyeball it: python3 route_demo.py 659 --route
  5. commit the golden, the code and the reason together.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import app
import route_demo as rd

client = TestClient(app)


@pytest.fixture(scope="module")
def golden():
    return json.load(open("data/659/golden_route.json"))


@pytest.fixture(scope="module")
def live(golden):
    return client.post("/api/route", json={"items": golden["items"]}).json()


def test_stop_sequence_is_frozen(golden, live):
    """Item, order and anchor — the actual shopping instruction."""
    got = [(s["n"], s["item"], s["anchor"]) for s in live["stops"]]
    want = [(s["n"], s["item"], s["anchor"]) for s in golden["stops"]]
    assert got == want


def test_stop_positions_are_frozen(golden, live):
    """Where each pin is drawn, including whether it is an approximation."""
    for want, got in zip(golden["stops"], live["stops"]):
        assert got["approx"] == want["approx"], want["item"]
        assert abs(got["x"] - want["x"]) < 0.01, want["item"]
        assert abs(got["y"] - want["y"]) < 0.01, want["item"]


def test_drawn_line_is_frozen(golden, live):
    """The polyline verbatim, not a hash — a diff should say WHERE it moved."""
    assert len(live["path"]) == len(golden["path"])
    for (gx, gy), (x, y) in zip(golden["path"], live["path"]):
        assert abs(x - gx) < 0.01 and abs(y - gy) < 0.01


def test_distance_and_savings_are_frozen(golden, live):
    assert live["distance_m"] == golden["distance_m"]
    assert live["baseline_m"] == golden["baseline_m"]
    assert live["saved_m"] == golden["saved_m"]


def test_unmatched_set_is_frozen(golden, live):
    """A resolver change that quietly starts or stops placing an item shows up
    here rather than in someone's basket."""
    assert [u["query"] for u in live["unmatched"]] == golden["unmatched"]


def test_route_request_meets_the_latency_budget(golden):
    """§8.7: <300 ms p95 for a route request."""
    import time
    client.post("/api/route", json={"items": golden["items"]})      # warm
    ts = []
    for _ in range(12):
        t0 = time.perf_counter()
        client.post("/api/route", json={"items": golden["items"]})
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    p95 = ts[int(len(ts) * 0.95) - 1]
    assert p95 < 300, f"p95 {p95:.0f} ms (p50 {ts[len(ts)//2]:.0f} ms)"
