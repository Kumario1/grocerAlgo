import numpy as np
from router import engine

TOY_GEOM = {"page": {"w": 40.0, "h": 40.0},
            "fixtures": [[16, 8, 24, 32]],          # one shelf block mid-map
            "obstacle_paths": [], "anchors": {}}

def test_build_grid_blocks_fixture_interior():
    free = engine.build_grid(TOY_GEOM, cell=4.0)
    assert free.shape == (10, 10)
    assert not free[5, 5]          # inside the fixture
    assert free[0, 0] and free[9, 9]

def test_bfs_routes_around_obstacle():
    free = engine.build_grid(TOY_GEOM, cell=4.0)
    dist, parent = engine.bfs(free, (0, 5))         # left of shelf
    w = free.shape[1]
    d = dist[5 * w + 9]                             # right of shelf, same row
    assert d > 9                                    # forced detour, not straight line

def test_snap_finds_nearest_walkable():
    free = engine.build_grid(TOY_GEOM, cell=4.0)
    dist, _ = engine.bfs(free, (0, 0))
    cell = engine.snap(free, dist, (20.0, 20.0))    # center of the fixture
    assert free[cell[1], cell[0]] and dist[cell[1] * free.shape[1] + cell[0]] >= 0

def test_held_karp_beats_naive_order():
    # start=0 end=1, stops 2,3,4 laid on a line: 0..2..3..4..1 optimal
    #  coords: 0@0, 2@1, 3@2, 4@3, 1@4
    pos = [0, 4, 1, 2, 3]
    D = [[abs(a - b) for b in pos] for a in pos]
    cost, order = engine.held_karp(D, 3)
    assert cost == 4 and order == [2, 3, 4]

def test_tsp_order_heuristic_over_18_stops():
    pos = list(range(25))                            # 0=start,1=end at 24
    pos[1] = 24
    pos[24] = 1
    D = [[abs(a - b) for b in pos] for a in pos]
    cost, order = engine.tsp_order(D, 23)
    assert cost <= 30                                # near-linear sweep, not garbage
    assert sorted(order) == list(range(2, 25))

def test_trace_returns_pdf_points():
    free = engine.build_grid(TOY_GEOM, cell=4.0)
    _, parent = engine.bfs(free, (0, 0))
    path = engine.trace(parent, free.shape[1], (5, 0), cell=4.0)
    assert path[0] == (2.0, 2.0) and path[-1] == (22.0, 2.0)
