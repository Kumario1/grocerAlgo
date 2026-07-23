"""Corridor graph: medial-axis skeleton of the walkable grid -> node/edge graph.

plan.md §8.1. The point of this module is to stop treating an aisle as the dot
where its number happens to be printed — which is at the aisle *mouth*, so a
router aiming at it walks past the aisle instead of into it. Skeletonising the
walkable floor gives the centreline of every aisle and cross-aisle; each aisle
becomes a SEGMENT with two real endpoints, which is what lets items be ordered
along it and entered from the correct end.

Build-time only. Nothing here runs in a route request.

NOT a source of drawn routes. The medial axis steps diagonally between pixels,
and on #659 about 9% of edges take one such step through a corner whose two
flanking cells are both blocked — geometrically fine for a centreline, but a
gap no shopper fits through. Routes stay on the grid (engine.bfs + trace +
string_pull), where every drawn segment is legality-checked.
"""
import numpy as np
from scipy import ndimage

from router import engine

# Universal constants (never per-store), in PDF points so a CELL change
# rescales them the way engine.py's thresholds do.
SPUR_PT = 16.0        # ~1.9 m: shorter dead-end branches are skeleton fuzz,
                      # not real aisle stubs
SLACK_PT = 24.0       # ~2.8 m: how far past the nearest edge project() will
                      # look for a longer one (see project)

_K8 = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.uint8)
_S8 = np.ones((3, 3), bool)


def skeleton(free):
    """Medial axis of the walkable region (one pixel wide)."""
    from skimage.morphology import medial_axis      # build-time dep only
    return medial_axis(free)


def _degree(sk):
    return ndimage.convolve(sk.astype(np.uint8), _K8, mode="constant") * sk


def _order_chain(pixels, start):
    """Order a one-pixel-wide chain by walking it from `start`.

    Prefers 4-connected steps: on a diagonal staircase the two pixels flanking
    a step are also 8-adjacent to each other, so a naive walk can jump the
    middle pixel and strand it. Taking the 4-neighbour first always consumes
    the staircase in order.
    """
    todo = set(pixels)
    todo.discard(start)
    out, cur = [start], start
    while todo:
        y, x = cur
        nxt = None
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0),      # 4-connected first
                       (1, 1), (1, -1), (-1, 1), (-1, -1)):
            if (y + dy, x + dx) in todo:
                nxt = (y + dy, x + dx)
                break
        if nxt is None:
            break                                   # chain ended (or forked)
        todo.discard(nxt)
        out.append(nxt)
        cur = nxt
    return out, todo                                # leftovers = fork remnant


def build(free, cell=engine.CELL):
    """Walkable grid -> {nodes, edges}.

    nodes: (k, 2) float array of (x, y) PDF points.
    edges: list of {"a", "b", "pts" (N,2 PDF points, a->b), "length" (points)}.
    """
    sk = skeleton(free)
    deg = _degree(sk)
    node_px = sk & (deg != 2)                        # ends (1) and junctions (3+)
    node_lab, n_nodes = ndimage.label(node_px, _S8)  # cluster fat junctions
    if n_nodes == 0:
        return {"nodes": np.zeros((0, 2)), "edges": []}

    cen = ndimage.center_of_mass(node_px, node_lab, range(1, n_nodes + 1))
    nodes = np.array([(x * cell + cell / 2, y * cell + cell / 2)
                      for y, x in cen])

    def pt(yx):
        return (yx[1] * cell + cell / 2, yx[0] * cell + cell / 2)

    chain_lab, n_chain = ndimage.label(sk & ~node_px, _S8)
    edges, orphan_chains = [], 0
    for c in range(1, n_chain + 1):
        pix = [tuple(p) for p in np.argwhere(chain_lab == c)]
        # a chain pixel can touch SEVERAL node clusters — a one-pixel chain
        # bridging two junctions touches both, and collapsing it to one would
        # silently drop that connection and split the graph
        touch = {}
        for y, x in pix:
            win = node_lab[max(0, y - 1):y + 2, max(0, x - 1):x + 2]
            hit = set(np.unique(win[win > 0]).tolist())
            if hit:
                touch[(y, x)] = hit
        if not touch:
            orphan_chains += 1                       # closed ring, no junction
            continue
        start = min(touch, key=lambda q: (min(touch[q]), q))
        ordered, _ = _order_chain(pix, start)
        a = min(touch[start])
        far = touch.get(ordered[-1], {a})
        b = min(far - {a}) if far - {a} else min(far)
        pts = [nodes[a - 1]] + [pt(q) for q in ordered] + [nodes[b - 1]]
        edges.append({"a": a - 1, "b": b - 1, "pts": np.array(pts, float)})

    # junction clusters that touch with no chain between them
    for a, b in _adjacent_node_pairs(node_lab, n_nodes):
        edges.append({"a": a, "b": b,
                      "pts": np.array([nodes[a], nodes[b]], float)})

    for e in edges:
        e["length"] = float(np.hypot(*np.diff(e["pts"], axis=0).T).sum())
    g = {"nodes": nodes, "edges": edges, "orphan_chains": orphan_chains}
    return prune_spurs(g, cell)


def _adjacent_node_pairs(node_lab, n_nodes):
    """Pairs of distinct node clusters sitting 8-adjacent to each other."""
    pairs = set()
    ys, xs = np.nonzero(node_lab)
    h, w = node_lab.shape
    for y, x in zip(ys, xs):
        me = node_lab[y, x]
        win = node_lab[max(0, y - 1):y + 2, max(0, x - 1):x + 2]
        for other in np.unique(win):
            if other > 0 and other != me:
                pairs.add((min(me, other) - 1, max(me, other) - 1))
    return sorted(pairs)


def prune_spurs(g, cell=engine.CELL, spur_pt=SPUR_PT):
    """Drop short dead-end branches, never disconnecting the graph.

    The medial axis of a real store grows a hair into every shelf nook and
    doorway recess — on #659, 163 endpoints where the floorplan has perhaps 60
    real aisle ends. Each hair would otherwise become a node an anchor could
    snap to. An edge is only cut when one of its ends is a leaf, so removing it
    cannot split the graph.
    """
    edges, nodes = list(g["edges"]), g["nodes"]
    changed = True
    while changed:
        changed = False
        deg = {}
        for i, e in enumerate(edges):
            deg.setdefault(e["a"], []).append(i)
            deg.setdefault(e["b"], []).append(i)
        for i, e in enumerate(edges):
            leaf = len(deg.get(e["a"], ())) == 1 or len(deg.get(e["b"], ())) == 1
            if leaf and e["length"] < spur_pt and len(edges) > 1:
                edges.pop(i)
                changed = True
                break
    return {"nodes": nodes, "edges": edges,
            "orphan_chains": g.get("orphan_chains", 0)}


def smooth_edges(g, free, cell=engine.CELL):
    """Pull each edge polyline taut against the walkable grid.

    The medial axis of a pixel grid zigzags one cell at a time, which inflates
    every edge length. Reuses the route smoother, so an edge is straightened
    only where a shopper could really walk straight.
    """
    for e in g["edges"]:
        pts = [tuple(p) for p in e["pts"]]
        pulled = engine.string_pull(free, pts, cell)
        if engine.path_is_legal(free, pulled, cell) == []:
            e["pts"] = np.array(pulled, float)
            e["length"] = float(np.hypot(*np.diff(e["pts"], axis=0).T).sum())
    return g


def adjacency(g):
    """node -> [(neighbour, length, edge_id)]"""
    adj = {}
    for i, e in enumerate(g["edges"]):
        if e["a"] == e["b"]:
            continue
        adj.setdefault(e["a"], []).append((e["b"], e["length"], i))
        adj.setdefault(e["b"], []).append((e["a"], e["length"], i))
    return adj


def distances(g, placed):
    """All-pairs distance between projected anchors, walking the corridors.

    Each anchor sits at t along its own edge, so a search starts from BOTH
    endpoints of that edge at their partial-edge costs. Returns (names, D) with
    D in PDF points. Build-time only — §8.4 wants the request path to be
    nothing but lookups in this matrix.
    """
    import heapq

    adj = adjacency(g)
    names = sorted(placed)
    D = np.full((len(names), len(names)), np.inf)
    for i, name in enumerate(names):
        a = placed[name]
        ea = g["edges"][a["edge"]]
        dist, pq = {}, [(a["t"] * ea["length"], ea["a"]),
                        ((1 - a["t"]) * ea["length"], ea["b"])]
        heapq.heapify(pq)
        while pq:
            d, u = heapq.heappop(pq)
            if u in dist:
                continue
            dist[u] = d
            for v, w, _ in adj.get(u, ()):
                if v not in dist:
                    heapq.heappush(pq, (d + w, v))
        for j, other in enumerate(names):
            b = placed[other]
            eb = g["edges"][b["edge"]]
            cand = []
            if eb["a"] in dist:
                cand.append(dist[eb["a"]] + b["t"] * eb["length"])
            if eb["b"] in dist:
                cand.append(dist[eb["b"]] + (1 - b["t"]) * eb["length"])
            if b["edge"] == a["edge"]:               # same segment: just walk it
                cand.append(abs(b["t"] - a["t"]) * ea["length"])
            if cand:
                D[i, j] = min(cand)
    return names, D


def _project_edge(e, xy):
    """(distance, t, point) for the closest point of one edge polyline."""
    p = e["pts"]
    seg = np.diff(p, axis=0)
    L2 = (seg ** 2).sum(1)
    L2[L2 == 0] = 1e-12
    u = np.clip(((xy - p[:-1]) * seg).sum(1) / L2, 0, 1)
    proj = p[:-1] + u[:, None] * seg
    d = np.hypot(*(proj - xy).T)
    k = int(np.argmin(d))
    steps = np.hypot(*seg.T)
    along = steps[:k].sum() + steps[k] * u[k]
    return float(d[k]), float(along / (steps.sum() or 1.0)), tuple(proj[k])


def project(g, xy, slack=SLACK_PT):
    """Nearest point on the corridor graph, preferring a substantial corridor.

    Returns (edge_id, t, point): "AISLE 14 is a segment and the anchor sits at
    t along it", the §8.1 payload.

    The plain nearest edge is the wrong answer here. An aisle badge is printed
    at the aisle MOUTH, which is exactly where the aisle meets a cross-aisle —
    a junction, surrounded by short clutter edges. Snapping to the nearest edge
    put 41 of #659's 45 aisle anchors on a ~20 pt stub instead of the centreline
    running down the aisle, which is the segment we actually want. So among the
    edges that come within `slack` of the closest one, take the longest.
    """
    cands = []
    for ei, e in enumerate(g["edges"]):
        d, t, pt = _project_edge(e, xy)
        cands.append((d, ei, t, pt, e["length"]))
    dmin = min(c[0] for c in cands)
    near = [c for c in cands if c[0] <= dmin + slack]
    d, ei, t, pt, _ = max(near, key=lambda c: c[4])
    return ei, t, pt


def assign(g, anchors):
    """Every anchor projected onto its corridor segment."""
    return {name: dict(zip(("edge", "t", "xy"), project(g, np.asarray(xy, float))))
            for name, xy in anchors.items()}
