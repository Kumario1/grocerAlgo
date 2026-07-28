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

def test_opening_clears_only_a_wall_not_a_fixture():
    geom = {**TOY_GEOM, "obstacle_paths": [[[0, 20], [40, 20]]]}
    opening = [{"rect": [8, 18, 22, 22]}]
    free = engine.build_grid(geom, cell=4.0, openings=opening)
    assert free[5, 3] and not free[5, 6]
    assert not free[5, 5]             # opening overlaps but cannot erase shelf

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

def test_held_karp_matches_brute_force():
    """The vectorised DP against exhaustive enumeration.

    Checks the returned ORDER actually walks the returned COST, not just that
    the numbers agree — a bad backtrack gives the right cost with the wrong
    sequence, which the map would happily draw.
    """
    import itertools
    rng = np.random.default_rng(7)
    for _ in range(20):
        n = int(rng.integers(1, 7))
        pts = rng.random((n + 2, 2)) * 100
        D = np.round(np.abs(pts[:, None, :] - pts[None, :, :]).sum(-1)).astype(int).tolist()
        cost, order = engine.held_karp(D, n)
        walk = lambda o: (D[0][o[0]] + sum(D[a][b] for a, b in zip(o, o[1:]))
                          + D[o[-1]][1])
        best = min(walk(list(p)) for p in itertools.permutations(range(2, 2 + n)))
        assert sorted(order) == list(range(2, 2 + n))     # every stop, once
        assert abs(cost - best) < 1e-4                    # optimal
        assert walk(order) == best                        # and the order proves it

def test_tsp_order_is_exact_up_to_18_stops():
    """§8.5's cutoff. The 25-item acceptance list consolidates to exactly 18,
    so this is the difference between solving it exactly and approximating it."""
    rng = np.random.default_rng(3)
    pts = rng.random((20, 2)) * 100
    D = np.round(np.abs(pts[:, None, :] - pts[None, :, :]).sum(-1)).astype(int).tolist()
    exact, _ = engine.held_karp(D, 18)
    cost, order = engine.tsp_order(D, 18)
    assert cost == exact and sorted(order) == list(range(2, 20))

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
