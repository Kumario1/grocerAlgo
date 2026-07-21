"""Routing engine: occupancy grid, BFS distances, fixed-endpoint TSP."""
import numpy as np
from collections import deque
from PIL import Image, ImageDraw
from scipy import ndimage

def build_grid(geom, cell=4.0, exclusions=()):
    """True = walkable.

    Walkable space starts as the interior of the sales-floor boundary
    polygon (geom["boundary"]; everything outside — parking, drive-thru,
    curbside — is blocked). Fixtures block their interior, walls their
    line, and exclusion shapes (hand-QA zones the drawing shows open but
    shoppers can't use, e.g. behind-Dairy) block like fixtures.
    `exclusions` is a list of {"rect": ...} / {"poly": ...} entries
    (see shape_mask). Geometries without a boundary (toy tests) start
    fully walkable.
    """
    w = int(np.ceil(geom["page"]["w"] / cell))
    h = int(np.ceil(geom["page"]["h"] / cell))
    if geom.get("boundary"):
        mask = Image.new("1", (w, h), 0)
        ImageDraw.Draw(mask).polygon(
            [(x / cell, y / cell) for x, y in geom["boundary"]], fill=1)
        free = np.array(mask, bool)
    else:
        free = np.ones((h, w), bool)
    for x0, y0, x1, y1 in geom["fixtures"]:
        free[int(y0 // cell):int(np.ceil(y1 / cell)),
             int(x0 // cell):int(np.ceil(x1 / cell))] = False
    if geom.get("fixture_polys"):
        # exact non-rect furniture (diagonal counters, curved kiosks)
        free &= ~shape_mask([{"poly": p} for p in geom["fixture_polys"]],
                            (h, w), cell)
    if exclusions:
        free &= ~shape_mask(exclusions, (h, w), cell)
    for (x0, y0), (x1, y1) in geom["obstacle_paths"]:
        n = max(2, int(max(abs(x1 - x0), abs(y1 - y0)) / cell) * 2)
        for t in np.linspace(0, 1, n):
            cx, cy = int((x0 + t * (x1 - x0)) // cell), int((y0 + t * (y1 - y0)) // cell)
            if 0 <= cy < h and 0 <= cx < w:
                free[cy, cx] = False
    return free

def bfs(free, start):
    """4-connected BFS from a (x, y) cell. Returns flat (dist, parent) int32."""
    h, w = free.shape
    dist = np.full(h * w, -1, np.int32)
    parent = np.full(h * w, -1, np.int32)
    s = start[1] * w + start[0]
    dist[s] = 0
    q = deque([s])
    flat_free = free.ravel()
    while q:
        u = q.popleft()
        ux, uy = u % w, u // w
        for v in (u - 1 if ux > 0 else -1, u + 1 if ux < w - 1 else -1,
                  u - w if uy > 0 else -1, u + w if uy < h - 1 else -1):
            if v >= 0 and flat_free[v] and dist[v] < 0:
                dist[v] = dist[u] + 1
                parent[v] = u
                q.append(v)
    return dist, parent

def seal_staff_gaps(free, seed_pt, cell=4.0, gap_kernel=2, max_pocket_cells=800,
                    protect_pts=()):
    """Cull staff-only service interiors (deli/bakery islands, seafood
    counters...) that connect to the sales floor only through narrow
    staff pass-throughs.

    H-E-B maps draw service counters with small gaps (~1-2 cells = 0.5-1 m)
    that shoppers never use; customer openings are >=3 cells (~1.5 m+).
    Recipe: morphologically bridge gaps <= gap_kernel cells, BFS from the
    seed on the sealed grid, and cull the regions that became unreachable —
    but only pockets <= max_pocket_cells, so an over-aggressive kernel can
    disconnect a huge region without silently deleting it.
    Side effect (accepted, conservative): 2-cell checkout lanes seal too.

    protect_pts: PDF-point coordinates that mark known-customer space
    (aisle badges). A pocket within PROTECT_R cells of one is a narrow
    shopping aisle, not a staff area — never culled. Real aisles and
    staff gaps can both be ~2 cells wide at this resolution; the badge
    is the tiebreaker. Badges print at the corridor MOUTH (which stays
    in the main region), so a small window is needed, but it must stay
    tight: the wine-back staff strip sits ~4.5 cells from badge 14.
    """
    PROTECT_R = 4
    st = np.ones((gap_kernel + 1, gap_kernel + 1), bool)
    sealed = free & ~ndimage.binary_closing(~free, structure=st)
    seed = nearest_free(sealed, seed_pt, cell)          # doorway-pinch safe
    dist, _ = bfs(sealed, seed)
    h, w = free.shape
    pockets = free & ~(dist >= 0).reshape(h, w)
    labels, n = ndimage.label(pockets)
    protected = set()
    for px, py in protect_pts:
        cx, cy = int(px // cell), int(py // cell)
        win = labels[max(0, cy - PROTECT_R):cy + PROTECT_R + 1,
                     max(0, cx - PROTECT_R):cx + PROTECT_R + 1]
        protected |= set(np.unique(win[win > 0]).tolist())
    out = free.copy()
    culled = []
    for i in range(1, n + 1):
        m = labels == i
        size = int(m.sum())
        if i in protected and size >= 25:
            continue        # badge-adjacent AND aisle-sized -> real corridor
        if size <= max_pocket_cells:
            out[m] = False
            if size >= 25:                              # ignore corner nibbles
                ys, xs = np.where(m)
                culled.append((size, float(xs.mean() * cell),
                               float(ys.mean() * cell)))
    return out, culled

def shape_mask(entries, shape, cell=4.0):
    """Bool grid mask from exclusion/inclusion entries.

    Each entry carries either "rect": [x0, y0, x1, y1] (fine for aisle-side
    bands) or "poly": [[x, y], ...] (vertex list, for slanted/stepped areas
    like the deli island that a rectangle can't follow). PDF points.
    """
    m = np.zeros(shape, bool)
    poly_img, poly_draw = None, None
    for e in entries:
        if "rect" in e:
            x0, y0, x1, y1 = e["rect"]
            m[int(y0 // cell):int(np.ceil(y1 / cell)),
              int(x0 // cell):int(np.ceil(x1 / cell))] = True
        elif "poly" in e:
            if poly_img is None:
                poly_img = Image.new("1", (shape[1], shape[0]), 0)
                poly_draw = ImageDraw.Draw(poly_img)
            poly_draw.polygon([(x / cell, y / cell) for x, y in e["poly"]],
                              fill=1)
    if poly_img is not None:
        m |= np.array(poly_img, bool)
    return m

def nearest_free(free, xy_pt, cell=4.0):
    """Nearest walkable cell to a PDF-point coordinate (ignores reachability).
    Use to seed the first BFS when the reference point (e.g. ENTRANCE, drawn
    on the boundary line itself) may not be walkable."""
    cx, cy = int(xy_pt[0] // cell), int(xy_pt[1] // cell)
    h, w = free.shape
    for r in range(120):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = cx + dx, cy + dy
                if 0 <= x < w and 0 <= y < h and free[y, x]:
                    return (x, y)
    raise ValueError(f"no walkable cell near {xy_pt}")

def snap(free, reachable_dist, xy_pt, cell=4.0):
    """Nearest walkable-and-reachable cell to a PDF-point coordinate."""
    cx, cy = int(xy_pt[0] // cell), int(xy_pt[1] // cell)
    h, w = free.shape
    for r in range(80):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = cx + dx, cy + dy
                if 0 <= x < w and 0 <= y < h and reachable_dist[y * w + x] >= 0:
                    return (x, y)
    raise ValueError(f"no walkable cell near {xy_pt}")

def held_karp(D, n_stops):
    """Fixed start (index 0) and end (index 1); stops are indices 2..n+1."""
    S = list(range(2, 2 + n_stops))
    full = (1 << n_stops) - 1
    dp = {}
    for j in range(n_stops):
        dp[(1 << j, j)] = (D[0][S[j]], -1)
    for mask in range(1, full + 1):
        for j in range(n_stops):
            if not mask & (1 << j) or (mask, j) not in dp:
                continue
            base = dp[(mask, j)][0]
            for k in range(n_stops):
                if mask & (1 << k):
                    continue
                nm = mask | (1 << k)
                cand = base + D[S[j]][S[k]]
                if (nm, k) not in dp or cand < dp[(nm, k)][0]:
                    dp[(nm, k)] = (cand, j)
    best, last = min((dp[(full, j)][0] + D[S[j]][1], j) for j in range(n_stops))
    order, mask = [], full
    while last != -1:
        order.append(last)
        mask, last = mask ^ (1 << last), dp[(mask, last)][1]
    return best, [S[j] for j in reversed(order)]

def tsp_order(D, n_stops):
    """Exact <=14 stops; nearest-neighbor + 2-opt beyond (plan.md §8.5).

    Cutoff dropped from 18 to 14 per the Task 7 perf guard: pure-Python
    Held-Karp on 18 stops is ~5 s (blows the <1 s G3 budget); the 2-opt
    branch is within a few % of optimal for 15-18 stops.
    """
    if n_stops <= 14:
        return held_karp(D, n_stops)
    S = list(range(2, 2 + n_stops))
    order, left, cur = [], set(S), 0
    while left:                                   # nearest neighbor from start
        nxt = min(left, key=lambda j: D[cur][j])
        order.append(nxt); left.remove(nxt); cur = nxt
    def cost(o):
        return (D[0][o[0]] + sum(D[a][b] for a, b in zip(o, o[1:])) + D[o[-1]][1])
    improved = True
    while improved:                               # 2-opt
        improved = False
        for i in range(len(order) - 1):
            for j in range(i + 1, len(order)):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if cost(cand) < cost(order):
                    order, improved = cand, True
    return cost(order), order

def trace(parent, w, end_cell, cell=4.0):
    """Walk parents back from end_cell; return PDF-point path start->end."""
    path, u = [], end_cell[1] * w + end_cell[0]
    while u != -1:
        path.append((u % w * cell + cell / 2, u // w * cell + cell / 2))
        u = parent[u]
    return path[::-1]
