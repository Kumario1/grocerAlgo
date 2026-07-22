"""Route legality: a drawn path must never cross something you can't walk through.

Two independent gates, because they fail differently:
  - vs the FREE GRID    catches a router that draws through its own obstacles;
  - vs the RAW FIXTURES catches the §8.1 leak class, where the free grid itself
    is wrong (a hairline gap let walkable space bleed into a shelf) and a route
    through that shelf therefore passes the first gate happily.
"""
import json

import numpy as np
import pytest

from router import engine
import route_demo as rd


def grid(h, w):
    return np.ones((h, w), bool)


# --- line of sight ------------------------------------------------------

def test_clear_on_open_floor():
    assert engine.clear(grid(5, 5), (0.5, 0.5), (4.5, 4.5), cell=1.0)


def test_clear_blocked_by_wall():
    free = grid(5, 5)
    free[:, 2] = False
    assert not engine.clear(free, (0.5, 2.5), (4.5, 2.5), cell=1.0)


def test_clear_cannot_tunnel_through_a_one_cell_wall():
    """The reason for grid-marching instead of sampling the segment N times:
    a sampler steps over a thin wall between two samples."""
    free = grid(3, 20)
    free[:, 10] = False
    assert not engine.clear(free, (0.5, 1.5), (19.5, 1.5), cell=1.0)


def test_clear_refuses_the_diagonal_corner_squeeze():
    """Two obstacles touching at a corner leave a zero-width gap. Supercover
    treats that as blocked; a thin Bresenham line would slip through."""
    free = grid(3, 3)
    free[0, 1] = free[1, 0] = False
    assert not engine.clear(free, (0.5, 0.5), (1.5, 1.5), cell=1.0)


def test_clear_needs_both_flanks_open_at_a_corner():
    free = grid(3, 3)
    free[0, 1] = False                      # one flank blocked is still a squeeze
    assert not engine.clear(free, (0.5, 0.5), (1.5, 1.5), cell=1.0)
    free[0, 1] = True
    assert engine.clear(free, (0.5, 0.5), (1.5, 1.5), cell=1.0)


# --- path_is_legal ------------------------------------------------------

def test_path_is_legal_reports_which_segment_is_bad():
    free = grid(5, 5)
    free[0:4, 2] = False                               # wall with a gap at the bottom
    path = [(0.5, 0.5), (0.5, 4.5), (4.5, 4.5)]        # legal: goes around
    assert engine.path_is_legal(free, path, cell=1.0) == []
    path = [(0.5, 0.5), (4.5, 0.5), (4.5, 4.5)]        # segment 0 crosses the wall
    assert engine.path_is_legal(free, path, cell=1.0) == [0]


def test_path_is_legal_checks_a_lone_vertex():
    free = grid(3, 3)
    free[1, 1] = False
    assert engine.path_is_legal(free, [(0.5, 0.5)], cell=1.0) == []
    assert engine.path_is_legal(free, [(1.5, 1.5)], cell=1.0) == [0]


# --- string_pull --------------------------------------------------------

def test_string_pull_collapses_a_staircase():
    free = grid(10, 10)
    stair = [(x + 0.5, y + 0.5) for x in range(9) for y in (x, x + 1)]
    out = engine.string_pull(free, stair, cell=1.0)
    assert len(out) < len(stair)
    assert engine.path_is_legal(free, out, cell=1.0) == []


def test_string_pull_keeps_the_turn_it_needs():
    free = grid(5, 5)
    free[0:4, 2] = False                     # detour is forced through the gap
    around = [(0.5, 0.5), (0.5, 4.5), (2.5, 4.5), (4.5, 4.5), (4.5, 0.5)]
    out = engine.string_pull(free, around, cell=1.0)
    assert engine.path_is_legal(free, out, cell=1.0) == []
    assert len(out) >= 3                     # cannot become a straight line


def test_whole_route_smoothing_is_legal_but_eats_the_stops():
    """Why string_pull is documented per-leg-only.

    On open floor the greedy skip sees straight from the first vertex to the
    last and swallows everything between — a perfectly legal route that visits
    nothing. Legality and 'still goes where it was sent' are separate
    invariants; this pins that smoothing the concatenated route is wrong.
    """
    free = grid(20, 20)
    stop = (19.5, 0.5)                       # the corner the route must visit
    leg1 = [(x + 0.5, 0.5) for x in range(20)]            # entrance -> stop
    leg2 = [(19.5, y + 0.5) for y in range(20)]           # stop -> checkout
    whole = engine.string_pull(free, leg1 + leg2, cell=1.0)
    assert engine.path_is_legal(free, whole, cell=1.0) == []   # legal...
    assert stop not in whole                                   # ...and useless

    # legs share the stop vertex, so the join drops the duplicate, not the stop
    per_leg = engine.string_pull(free, leg1, cell=1.0) + \
        engine.string_pull(free, leg2, cell=1.0)[1:]
    assert stop in per_leg
    assert engine.path_is_legal(free, per_leg, cell=1.0) == []


# --- the real store -----------------------------------------------------

@pytest.fixture(scope="module")
def demo():
    prof = rd.load("659")
    return prof, rd.demo_paths(prof, rd.demo_pairs(prof, 6, seed=1))


@pytest.fixture(scope="module")
def furniture():
    """Blocks only what the guide draws — independent of the free-grid build."""
    geom = json.load(open("data/659/geometry.json"))
    return engine.build_grid({"page": geom["page"], "boundary": None,
                              "fixtures": geom["fixtures"],
                              "fixture_polys": geom["fixture_polys"],
                              "obstacle_paths": []})


def test_demo_paths_are_reachable_and_legal_on_the_free_grid(demo):
    prof, paths = demo
    assert len(paths) >= 9
    for p in paths:
        assert not p["unreachable"], p["label"]
        assert p["bad_raw"] == [], f"{p['label']} raw"
        assert p["bad_smooth"] == [], f"{p['label']} smoothed"


def test_demo_paths_never_cross_drawn_furniture(demo, furniture):
    prof, paths = demo
    for p in paths:
        assert engine.path_is_legal(furniture, p["smooth"], prof["cell"]) == [], \
            f"{p['label']} cuts through a shelf"


def test_live_route_is_legal(furniture):
    """The shipped route stays legal against both independent obstacle masks."""
    prof = rd.load("659")
    path, _, _ = rd.live_route()
    assert engine.path_is_legal(prof["free"], path, prof["cell"]) == []
    assert engine.path_is_legal(furniture, path, prof["cell"]) == []


def test_route_walks_in_and_actually_reaches_every_item():
    """The route must COLLECT the list, not just drive past it.

    Aisle numbers are printed at the aisle mouth, so routing anchor-to-anchor
    produced a line that ran along the cross-aisle past every aisle it was
    supposed to shop — items sat metres off the drawn path, in aisles the
    shopper never entered. Each item's standing spot must lie on the line.
    """
    prof = rd.load("659")
    path, stops, _ = rd.live_route()
    cell, mpc = prof["cell"], prof["m_per_cell"]
    items = json.load(open("data/659/shelf_positions.json"))["items"]
    pts = np.array(path, float)
    a, b = pts[:-1], pts[1:]
    seg = b - a
    L2 = (seg ** 2).sum(1)
    L2[L2 == 0] = 1e-9

    worst, checked = 0.0, 0
    for s in stops:
        hit = next((v for v in items.values()
                    if v.get("cell") and abs(v["x"] - s["x"]) < 0.01
                    and abs(v["y"] - s["y"]) < 0.01), None)
        if not hit:
            continue                    # guarded below: this must stay rare
        checked += 1
        cx, cy = hit["cell"]
        stand = np.array([cx * cell + cell / 2, cy * cell + cell / 2])
        u = np.clip(((stand - a) * seg).sum(1) / L2, 0, 1)
        d = np.hypot(*((a + u[:, None] * seg) - stand).T).min() / cell * mpc
        worst = max(worst, d)
        assert d < 2.0, f"{s['item']} is {d:.1f} m off the route"
    # a lookup that stops matching would make this test pass by checking nothing
    assert checked == len(stops), f"only resolved {checked}/{len(stops)} stops"
    assert worst < 2.0


def test_items_are_placed_on_their_own_shelf_not_the_aisle_mouth():
    """§8.3. An item with a printed category label must not sit on top of the
    aisle anchor — that collapse is exactly what per-item stops undo."""
    prof = rd.load("659")
    _, stops, _ = rd.live_route()
    cell, idx = prof["cell"], prof["idx"]
    exact = [s for s in stops if not s["approx"]]
    assert len(exact) >= len(stops) // 2, "most items should have a real label"
    moved = 0
    for s in exact:
        cx, cy = prof["cells"][idx[s["anchor"]]]
        ax, ay = float(cx) * cell + cell / 2, float(cy) * cell + cell / 2
        if abs(s["x"] - ax) > 1 or abs(s["y"] - ay) > 1:
            moved += 1
    assert moved == len(exact), "labelled items are still pinned to the anchor"


def test_smoothing_shortens_without_leaving_the_floor(demo):
    prof, paths = demo
    cell, mpc = prof["cell"], prof["m_per_cell"]
    for p in paths:
        raw, smooth = rd.length_m(p["raw"], cell, mpc), rd.length_m(p["smooth"], cell, mpc)
        assert len(p["smooth"]) <= len(p["raw"])
        assert smooth <= raw + 1e-6, f"{p['label']} got longer"


def test_demo_pairs_are_deterministic_for_a_seed():
    prof = rd.load("659")
    assert rd.demo_pairs(prof, 6, seed=1) == rd.demo_pairs(prof, 6, seed=1)
    assert rd.demo_pairs(prof, 6, seed=1) != rd.demo_pairs(prof, 6, seed=2)
