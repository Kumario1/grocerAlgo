"""Corridor graph (§8.1) and per-item shelf positions (§8.3)."""
import numpy as np
import pytest

from router import corridor, derive, engine, shelf


@pytest.fixture(scope="module")
def graph659():
    prof = np.load("data/659/profile.npz", allow_pickle=True)
    free, cell = prof["free"], float(prof["cell"])
    g = corridor.smooth_edges(corridor.build(free, cell), free, cell)
    return g, free, cell


def test_graph_is_one_connected_component(graph659):
    """A split graph silently strands anchors: a corridor that exists on the
    map but that no route can reach."""
    g, _, _ = graph659
    adj = corridor.adjacency(g)
    seed = next(iter(adj))
    seen, stack = {seed}, [seed]
    while stack:
        for v, _, _ in adj.get(stack.pop(), ()):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    assert len(seen) == len(adj)


def test_aisle_anchors_land_on_aisle_length_segments(graph659):
    """The §8.1 payload. An aisle badge is printed at the aisle MOUTH, which is
    a junction surrounded by short clutter edges — snapping to the plain
    nearest edge put 41 of 45 aisle anchors on a ~20 pt stub instead of the
    centreline running down the aisle."""
    g, _, _ = graph659
    anchors = derive.load_store("data/659")["anchors"]
    placed = corridor.assign(g, {k: v for k, v in anchors.items()
                                 if k.startswith("AISLE ")})
    lengths = np.array([g["edges"][p["edge"]]["length"] for p in placed.values()])
    assert (lengths > 100).sum() >= 0.85 * len(lengths)
    # and the badge sits at an END of its segment, not in the middle
    ends = [min(p["t"], 1 - p["t"]) for p in placed.values()]
    assert np.median(ends) < 0.15


def test_project_prefers_a_real_corridor_over_a_nearby_stub():
    g = {"edges": [
        {"pts": np.array([[0.0, 0.0], [6.0, 0.0]]), "length": 6.0,      # stub
         "a": 0, "b": 1},
        {"pts": np.array([[0.0, 10.0], [200.0, 10.0]]), "length": 200.0,  # aisle
         "a": 2, "b": 3},
    ], "nodes": np.zeros((4, 2))}
    edge, _, _ = corridor.project(g, np.array([3.0, 0.0]), slack=corridor.SLACK_PT)
    assert edge == 1                     # 10 pt further, but the actual corridor
    edge, _, _ = corridor.project(g, np.array([3.0, 0.0]), slack=2.0)
    assert edge == 0                     # with no slack, the nearest wins


def test_corridor_stays_on_walkable_floor(graph659):
    """Every centreline vertex sits on walkable floor.

    Deliberately weaker than path_is_legal: the medial axis steps diagonally
    between pixels, and ~9% of #659's edges take one such step through a corner
    whose two flanking cells are blocked. A shopper cannot squeeze through that
    zero-width gap, which is why routes are traced on the grid and legality
    checked there — the corridor graph is a structural abstraction, not a
    polyline anyone walks.
    """
    g, free, cell = graph659
    for e in g["edges"]:
        for x, y in e["pts"]:
            r, c = int(y // cell), int(x // cell)
            assert 0 <= r < free.shape[0] and 0 <= c < free.shape[1] and free[r, c]


def test_corridor_distances_agree_with_the_grid(graph659):
    """Sanity band, not equality: the corridor walks centrelines and the grid
    counts 4-connected cells, so they should land within a few percent. A big
    divergence means the graph took a shortcut the floor does not have."""
    g, _, cell = graph659
    prof = np.load("data/659/profile.npz", allow_pickle=True)
    anchors = derive.load_store("data/659")["anchors"]
    names_c, Dc = corridor.distances(g, corridor.assign(g, anchors))
    grid_idx = {str(n): i for i, n in enumerate(prof["names"])}
    common = [n for n in names_c if n in grid_idx]
    ci = [names_c.index(n) for n in common]
    gi = [grid_idx[n] for n in common]
    A = Dc[np.ix_(ci, ci)] / cell
    B = prof["D"][np.ix_(gi, gi)].astype(float)
    m = (B > 10) & np.isfinite(A)
    assert 0.9 < np.median(A[m] / B[m]) < 1.1


# --- shelf placement ----------------------------------------------------

def test_walk_order_follows_the_end_you_enter_from():
    seg = {"a": [0.0, 0.0], "b": [100.0, 0.0]}
    items = [{"t": 0.9, "x": 90, "y": 0}, {"t": 0.1, "x": 10, "y": 0}]
    assert shelf.walk_order(items, seg, (0.0, 0.0)) == [1, 0]
    assert shelf.walk_order(items, seg, (100.0, 0.0)) == [0, 1]


def test_duplicate_label_text_resolves_to_the_nearest_aisle():
    """#659 prints "Rice" in two aisles. Matching on text alone sent the item
    to whichever the fuzzy matcher happened to rank first — half the store away."""
    phrases = [("Rice", 560.0, 210.0), ("Rice", 700.0, 320.0)]
    anchors = {"AISLE 5": (710.0, 330.0)}
    out = shelf.positions({"Rice": ["AISLE 5"]}, anchors, phrases)
    assert (out["Rice"]["x"], out["Rice"]["y"]) == (700.0, 320.0)
    assert not out["Rice"]["approx"]


def test_item_with_no_printed_label_is_flagged_approx():
    out = shelf.positions({"Widgets": ["AISLE 5"]}, {"AISLE 5": (710.0, 330.0)},
                          [("Rice", 700.0, 320.0)])
    assert out["Widgets"]["approx"] and out["Widgets"]["x"] == 710.0


def test_a_label_across_the_store_is_not_borrowed():
    """Beyond MAX_PT the same word is a different department, not this aisle."""
    out = shelf.positions({"Rice": ["AISLE 5"]}, {"AISLE 5": (710.0, 330.0)},
                          [("Rice", 50.0, 50.0)])
    assert out["Rice"]["approx"]


def test_labels_on_one_text_line_split_into_separate_labels():
    class FakePage:
        def get_text(self, _):
            return [(0, 0, 30, 8, "Diabetic", 0, 0, 0),
                    (31, 0, 60, 8, "Aids", 0, 0, 1),
                    (90, 0, 130, 8, "Vitamins", 0, 0, 2)]
    out = shelf.label_phrases(FakePage())
    assert sorted(t for t, _, _ in out) == ["Diabetic Aids", "Vitamins"]
