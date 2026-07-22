"""Routing engine: occupancy grid, BFS distances, fixed-endpoint TSP."""
import numpy as np
from collections import deque
from PIL import Image, ImageDraw
from scipy import ndimage

# The one resolution knob (PDF-pt per grid cell). Was 4.0; halved to 2.0 to
# resolve thin walkable crevices between fixtures that outward-rounding ate at
# the coarser cell. Every cell-count threshold below derives from CELL, so this
# is the ONLY number to change for a different resolution.
CELL = 2.0

def build_grid(geom, cell=CELL, exclusions=()):
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

# departments whose staff operate behind counters; a label printed inside a
# sealed pocket identifies that pocket as a staff service area
SERVICE_DEPTS = ("DELI", "BAKERY", "SEAFOOD", "SUSHI", "KITCHEN",
                 "PHARMACY", "MEAL SIMPLE", "COOKING")

# chain-branding label aliases -> canonical department family. BLOOMS is
# H-E-B's floral-shop brand (store 24 prints "Blooms" where 659 prints
# "Floral"). Used by the seal-zone DERIVATION (router/derive.py) only —
# never by the substring pocket-culling above.
ALIASES = {"BLOOMS": "FLORAL"}

def seal_staff_gaps(free, seed_pt, cell=CELL, seal_zones=(), max_pocket_cells=None,
                    protect_pts=(), service_pts=()):
    """Cull staff-only service interiors (deli/bakery islands, seafood
    counters...) that connect to the sales floor only through narrow
    staff pass-throughs.

    Sealing is LOCALIZED to `seal_zones` so the fine grid's thin walkable
    crevices elsewhere are never bridged shut. Each zone closes gaps up to
    its own width — checkout lanes are wider than counter gaps, so their
    zone runs looser:
        {"pt": [x, y], "r": radius_pt, "bridge": pt}   disk zone, or
        {"rect": [x0, y0, x1, y1], "bridge": pt}       box zone
    (PDF points; an optional "name" is ignored). With no zones, one global
    ~1.4 m bridge is used (legacy behaviour).

    Recipe: inside each zone, morphologically bridge gaps <= its bridge
    width; BFS from the seed on the sealed grid; classify the regions that
    became unreachable:
      - pocket holds a service_pts label (DELI, BAKERY...) -> STAFF AREA,
        culled with no size cap (the printed label is authoritative);
      - pocket near a protect_pts badge and aisle-sized -> shopping
        corridor, kept;
      - otherwise -> dead crevice / checkout lane, culled if <=
        max_pocket_cells so an over-aggressive bridge can never silently
        delete a real region.

    protect_pts: PDF-point coordinates that mark known-customer space
    (aisle badges) — a badge-adjacent, aisle-sized pocket is a shopping
    corridor, never culled.

    Returns (walkable, culled_pockets, staff_mask) — staff_mask flags the
    cells culled because a service label sat inside them.
    """
    # cell-count thresholds derive from `cell` so a resolution change rescales
    # them automatically; physical intents were calibrated at the 4-pt cell.
    if max_pocket_cells is None:
        max_pocket_cells = round(12800 / cell ** 2)  # dead-crevice cap ~180 m^2
    PROTECT_R = max(1, round(16 / cell))             # badge window ~1.9 m
    min_sz = round(400 / cell ** 2)                  # aisle-sized / nibble floor ~5.6 m^2
    h, w = free.shape
    if not seal_zones:                               # legacy: one global ~1.4 m bridge
        seal_zones = [{"rect": [0, 0, w * cell, h * cell], "bridge": 12}]
    Y, X = np.ogrid[:h, :w]
    sealed = free.copy()
    for z in seal_zones:                             # bridge each zone at its own width
        k = max(1, round(z["bridge"] / cell))
        bridged = free & ~ndimage.binary_closing(
            ~free, structure=np.ones((k + 1, k + 1), bool))
        if "r" in z:
            cx, cy, rr = z["pt"][0] / cell, z["pt"][1] / cell, z["r"] / cell
            zm = (X - cx) ** 2 + (Y - cy) ** 2 <= rr ** 2
        else:
            x0, y0, x1, y1 = (v / cell for v in z["rect"])
            zm = (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)
        sealed[zm] = bridged[zm]
    seed = nearest_free(sealed, seed_pt, cell)          # doorway-pinch safe
    dist, _ = bfs(sealed, seed)
    pockets = free & ~(dist >= 0).reshape(h, w)
    labels, n = ndimage.label(pockets)
    protected = set()
    for px, py in protect_pts:
        cx, cy = int(px // cell), int(py // cell)
        win = labels[max(0, cy - PROTECT_R):cy + PROTECT_R + 1,
                     max(0, cx - PROTECT_R):cx + PROTECT_R + 1]
        protected |= set(np.unique(win[win > 0]).tolist())
    service = set()
    for px, py in service_pts:
        cx, cy = int(px // cell), int(py // cell)
        if 0 <= cy < h and 0 <= cx < w and labels[cy, cx] > 0:
            service.add(int(labels[cy, cx]))
    out = free.copy()
    staff = np.zeros_like(free)
    culled = []
    for i in range(1, n + 1):
        m = labels == i
        size = int(m.sum())
        if i in service:                    # staff area, label-identified
            out[m] = False
            staff |= m
            ys, xs = np.where(m)
            culled.append((size, float(xs.mean() * cell),
                           float(ys.mean() * cell)))
            continue
        if i in protected and size >= min_sz:
            continue        # badge-adjacent AND aisle-sized -> real corridor
        if size <= max_pocket_cells:
            out[m] = False
            if size >= min_sz:                          # ignore corner nibbles
                ys, xs = np.where(m)
                culled.append((size, float(xs.mean() * cell),
                               float(ys.mean() * cell)))
    return out, culled, staff

def shape_mask(entries, shape, cell=CELL):
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

def nearest_free(free, xy_pt, cell=CELL):
    """Nearest walkable cell to a PDF-point coordinate (ignores reachability).
    Use to seed the first BFS when the reference point (e.g. ENTRANCE, drawn
    on the boundary line itself) may not be walkable."""
    cx, cy = int(xy_pt[0] // cell), int(xy_pt[1] // cell)
    h, w = free.shape
    for r in range(round(480 / cell)):                 # ~57 m search cap
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = cx + dx, cy + dy
                if 0 <= x < w and 0 <= y < h and free[y, x]:
                    return (x, y)
    raise ValueError(f"no walkable cell near {xy_pt}")

def snap(free, reachable_dist, xy_pt, cell=CELL):
    """Nearest walkable-and-reachable cell to a PDF-point coordinate."""
    cx, cy = int(xy_pt[0] // cell), int(xy_pt[1] // cell)
    h, w = free.shape
    for r in range(round(320 / cell)):                 # ~38 m search cap
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = cx + dx, cy + dy
                if 0 <= x < w and 0 <= y < h and reachable_dist[y * w + x] >= 0:
                    return (x, y)
    raise ValueError(f"no walkable cell near {xy_pt}")

def held_karp(D, n_stops):
    """Exact open-path TSP. Fixed start (index 0) and end (index 1); stops are
    indices 2..n+1. Returns (cost, [stop indices in visit order]).

    dp[mask, j] = cheapest walk from the start that has collected the stop set
    `mask` and is standing at stop j. Masks are handled a popcount layer at a
    time, so an entire layer resolves in one gather + argmin rather than a
    Python loop over 2^n dict entries -- every mask in a layer depends only on
    the layer below it, so there is nothing to serialise.

    That vectorisation is what makes §8.5's n<=18 cutoff affordable: 18 stops
    costs ~130 ms here against ~3 s for the dict DP this replaced (which is why
    the cutoff had been lowered to 14). The 25-item acceptance list consolidates
    to exactly 18 stops, so it now solves exactly instead of heuristically.
    """
    if n_stops == 0:
        return D[0][1], []
    D = np.asarray(D, np.float32)      # exact for integer cell counts (< 2^24)
    S = np.arange(2, 2 + n_stops)
    Ds = D[np.ix_(S, S)]
    N, full = 1 << n_stops, (1 << n_stops) - 1
    dp = np.full((N, n_stops), np.inf, np.float32)
    par = np.full((N, n_stops), -1, np.int8)
    dp[1 << np.arange(n_stops), np.arange(n_stops)] = D[0, S]

    masks = np.arange(N)
    popcount = np.zeros(N, np.int16)
    for j in range(n_stops):
        popcount += ((masks >> j) & 1).astype(np.int16)
    for p in range(2, n_stops + 1):
        layer = masks[popcount == p]
        for j in range(n_stops):
            sel = layer[((layer >> j) & 1) == 1]        # ... that end at j
            if not sel.size:
                continue
            # arrive at j from every possible k; unreachable k stay inf
            cand = dp[sel ^ (1 << j)] + Ds[:, j]
            k = np.argmin(cand, 1)
            dp[sel, j] = cand[np.arange(sel.size), k]
            par[sel, j] = k

    total = dp[full] + D[S, 1]
    j, mask, order = int(np.argmin(total)), full, []
    best = float(total[j])
    while j != -1:
        order.append(j)
        nxt = int(par[mask, j])
        mask ^= 1 << j
        j = nxt
    return best, [int(S[j]) for j in reversed(order)]

def tsp_order(D, n_stops):
    """Exact <=18 stops; nearest-neighbor + 2-opt beyond (plan.md §8.5).

    Back up to the spec's 18 now that held_karp is vectorised (~130 ms at 18,
    inside the §8.7 300 ms budget). Raising it further is not free: the DP is
    O(n^2 * 2^n), so each extra stop roughly triples the cost -- 19 lands near
    300 ms on its own and 20 blows the budget outright.
    """
    if n_stops <= 18:
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

def trace(parent, w, end_cell, cell=CELL):
    """Walk parents back from end_cell; return PDF-point path start->end."""
    path, u = [], end_cell[1] * w + end_cell[0]
    while u != -1:
        path.append((u % w * cell + cell / 2, u // w * cell + cell / 2))
        u = parent[u]
    return path[::-1]

def clear(free, a, b, cell=CELL):
    """Line of sight: every cell the segment a->b touches is walkable.

    Grid-marching (Amanatides-Woo), not point sampling: sampling a segment at
    N places can step straight over a one-cell-thick shelf wall between two
    samples, and a route that tunnels through a shelf is exactly what §8.1
    calls the leak class. Marching visits every cell the segment enters, so a
    wall of any thickness stops it.

    Supercover, not a thin Bresenham line: when the segment crosses an exact
    cell corner, BOTH cells flanking that corner must be walkable too — a
    shopper cannot squeeze through the zero-width gap between two obstacles
    touching at a diagonal.
    """
    h, w = free.shape
    # float() up front: coords arrive as numpy scalars from trace(), and the
    # marching loop below is much faster on plain Python numbers.
    x0, y0 = float(a[0]) / cell, float(a[1]) / cell
    x1, y1 = float(b[0]) / cell, float(b[1]) / cell
    cx, cy, ex, ey = int(x0 // 1), int(y0 // 1), int(x1 // 1), int(y1 // 1)

    def ok(x, y):
        return 0 <= y < h and 0 <= x < w and free[y, x]

    if not ok(cx, cy):
        return False
    dx, dy = x1 - x0, y1 - y0
    sx = 1 if dx > 0 else -1 if dx < 0 else 0
    sy = 1 if dy > 0 else -1 if dy < 0 else 0
    inf = float("inf")
    # t = fraction of the segment consumed when the next cell border is met
    tx = ((cx + 1 if sx > 0 else cx) - x0) / dx if sx else inf
    ty = ((cy + 1 if sy > 0 else cy) - y0) / dy if sy else inf
    dtx = abs(1.0 / dx) if sx else inf          # ... and per whole cell after that
    dty = abs(1.0 / dy) if sy else inf
    for _ in range(abs(ex - cx) + abs(ey - cy) + 2):   # bounded: never spins
        if cx == ex and cy == ey:
            return True
        if abs(tx - ty) < 1e-9:                 # exact corner: no diagonal squeeze
            if not (ok(cx + sx, cy) and ok(cx, cy + sy)):
                return False
            cx, cy, tx, ty = cx + sx, cy + sy, tx + dtx, ty + dty
        elif tx < ty:
            cx, tx = cx + sx, tx + dtx
        else:
            cy, ty = cy + sy, ty + dty
        if not ok(cx, cy):
            return False
    return cx == ex and cy == ey

def path_is_legal(free, pts, cell=CELL):
    """Indices of segments that leave walkable floor. Empty list = legal.

    Returns the offenders rather than a bool so a failure says WHERE.
    """
    if len(pts) < 2:
        x, y = int(pts[0][0] // cell), int(pts[0][1] // cell)
        h, w = free.shape
        return [] if (0 <= y < h and 0 <= x < w and free[y, x]) else [0]
    return [i for i, (p, q) in enumerate(zip(pts, pts[1:]))
            if not clear(free, p, q, cell)]

def string_pull(free, pts, cell=CELL):
    """Pull a BFS staircase taut, keeping only the corners it really turns.

    ONE LEG AT A TIME. Smoothing a whole multi-stop route in one call is legal
    and wrong: a clear sightline from the entrance to some late vertex lets the
    greedy skip swallow the tour whole. Measured on #659 — 1265 pts / 294.7 m
    collapsed to 3 pts / 21 m, visiting none of the stops, and path_is_legal
    reported zero offending segments. Legality does not imply the route still
    goes where it was sent; per-leg smoothing pins the stop endpoints so it
    cannot skip one.

    ponytail: greedy farthest-visible skip, O(n^2) sightline checks per leg
    (~2 ms on a 659 leg). Swap in a shortcut graph only if legs get much longer.
    """
    if len(pts) < 3:
        return list(pts)
    out, i = [pts[0]], 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1 and not clear(free, pts[i], pts[j], cell):
            j -= 1
        out.append(pts[j])
        i = j
    return out
