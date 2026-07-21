#!/usr/bin/env python3
"""
Store route optimizer — proof of concept on a real store floor plan.

Pipeline:
  1. Parse the floor-plan image into an occupancy grid (walkable vs blocked)
  2. Auto-detect the numbered aisle markers -> anchor points for item snapping
  3. BFS on the walkable grid for true walking distances between all stops
  4. Exact TSP (Held-Karp) with fixed start (entrance) and fixed end (checkout)
  5. Draw the optimal route back onto the original map

In production, the item -> location assignments come from the store's own
product-location data (Kroger API officially; HEB via an agent). Here they
are hardcoded placeholders to demo the routing layer.
"""
import numpy as np
from collections import deque
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

MAP_PATH = '/mnt/user-data/uploads/1784584942650_image.png'
OUT_PATH = '/mnt/user-data/outputs/optimal_route.png'
F = 2                    # grid downsample factor (1 cell = F px)
PX_PER_M = 41 / 3.0      # adjacent aisle pitch ~41 px; assume ~3.0 m in reality

# Aisle number of each detected disc, in (y-band, x) sorted order — verified
# visually against the map's markers.
ORDER_TO_AISLE = [23, 24, 25, 26, 27, 28, 13, 12, 11, 10, 9, 8, 6, 5, 4, 3, 2,
                  7, 1, 29, 30, 31, 32, 33, 19, 18, 34, 22, 21, 20, 17, 16, 15,
                  14, 35, 36, 37, 38, 39, 40, 41]

# Hand-placed anchors for perimeter departments + entrance/checkout (px).
DEPTS = {
    'PRODUCE':  (1292, 900), 'SEAFOOD': (1236, 652), 'BAKERY': (1048, 668),
    'DELI':     (985, 345),  'DAIRY':   (348, 345),  'MARKET': (1083, 238),
    'PHARMACY': (183, 770),  'FLORAL':  (988, 862),
    'HEALTHY LIVING': (920, 957), 'MEAL SIMPLE': (1205, 975),
    'ENTRANCE': (1072, 1020), 'CHECKOUT': (660, 800),
}

# ---- demo shopping list (placeholder locations; agent/API fills these) ----
ITEMS = [
    ('Bananas',          'PRODUCE'),
    ('Shrimp',           'SEAFOOD'),
    ('Sourdough loaf',   'BAKERY'),
    ('Sliced turkey',    'DELI'),
    ('Milk',             'DAIRY'),
    ('Tortilla chips',   'AISLE 7'),
    ('Pasta',            'AISLE 10'),
    ('Ice cream',        'AISLE 16'),
    ('Paper towels',     'AISLE 30'),
    ('Toothpaste',       'AISLE 38'),
    ('Birthday candles', 'AISLE 22'),
]


def parse_map():
    """Return (image, free-cell grid, anchors dict name -> (px, py))."""
    im = Image.open(MAP_PATH).convert('RGB')
    W, H = im.size
    g = np.asarray(im.convert('L'))
    obstacle = g < 190              # shelf outlines (gray), text, fixtures

    # Aisle discs are solid near-black circles sitting in walkable corridors:
    # detect them as anchors, then erase them so they don't block the path.
    lab, _ = ndimage.label(g < 60)
    discs = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        if 12 <= h <= 45 and 12 <= w <= 45 and 0.6 <= w / h <= 1.6:
            m = lab[sl] == i
            if m.sum() / (h * w) > 0.5:
                discs.append(((sl[1].start + sl[1].stop) / 2,
                              (sl[0].start + sl[0].stop) / 2))
                obstacle[sl][m] = False
    discs.sort(key=lambda c: (round(c[1] / 25), c[0]))
    assert len(discs) == len(ORDER_TO_AISLE), f'found {len(discs)} discs'
    anchors = {f'AISLE {a}': d for a, d in zip(ORDER_TO_AISLE, discs)}
    anchors.update(DEPTS)

    free = ~(obstacle.reshape(H // F, F, W // F, F).max(axis=(1, 3)))
    return im, free, anchors


def bfs(free, start):
    """4-connected BFS. Returns (dist, parent) as flat int32 arrays."""
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
        for v in (u - 1 if ux > 0 else -1,
                  u + 1 if ux < w - 1 else -1,
                  u - w if uy > 0 else -1,
                  u + w if uy < h - 1 else -1):
            if v >= 0 and flat_free[v] and dist[v] < 0:
                dist[v] = dist[u] + 1
                parent[v] = u
                q.append(v)
    return dist, parent


def snap(free, reachable, p):
    """Snap px point to nearest walkable cell reachable from the entrance."""
    cx, cy = int(p[0] // F), int(p[1] // F)
    h, w = free.shape
    for r in range(60):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = cx + dx, cy + dy
                if 0 <= x < w and 0 <= y < h and reachable[y * w + x] >= 0:
                    return (x, y)
    raise ValueError(f'no walkable cell near {p}')


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


def trace(parent, w, end_cell):
    path, u = [], end_cell[1] * w + end_cell[0]
    while u != -1:
        path.append((u % w * F + F // 2, u // w * F + F // 2))
        u = parent[u]
    return path[::-1]


def main():
    im, free, anchors = parse_map()
    W, H = im.size
    h, w = free.shape

    # entrance-connected region defines what "walkable" means for snapping
    ent = (int(anchors['ENTRANCE'][0] // F), int(anchors['ENTRANCE'][1] // F))
    if not free[ent[1], ent[0]]:
        # nudge entrance itself onto free space first
        d0, _ = bfs(free, (ent[0], ent[1] + 3))
        ent = snap(free, d0, anchors['ENTRANCE'])
    reach, _ = bfs(free, ent)

    names = ['ENTRANCE', 'CHECKOUT'] + [loc for _, loc in ITEMS]
    cells = {n: snap(free, reach, anchors[n]) for n in set(names)}

    # distance matrix + parents from every node we might route from
    fields = {n: bfs(free, cells[n]) for n in set(names)}
    idx = {n: i for i, n in enumerate(names)}
    D = [[0] * len(names) for _ in names]
    for a in names:
        da, _ = fields[a]
        for b in names:
            bx, by = cells[b]
            D[idx[a]][idx[b]] = int(da[by * w + bx])

    best_cells, order = held_karp(D, len(ITEMS))
    naive_cells = (D[0][2] + sum(D[i][i + 1] for i in range(2, len(names) - 1))
                   + D[len(names) - 1][1])

    to_m = F / PX_PER_M
    print(f'Optimized route : {best_cells * to_m:6.0f} m')
    print(f'List-order route: {naive_cells * to_m:6.0f} m')
    print(f'Saved           : {100 * (1 - best_cells / naive_cells):.0f}%\n')

    stop_names = [names[i] for i in order]
    seq = ['ENTRANCE'] + stop_names + ['CHECKOUT']
    for i, s in enumerate(seq):
        item = next((it for it, loc in ITEMS if loc == s), '')
        print(f'{i:2d}. {s:<10s} {item}')

    # ---- render ----
    canvas = Image.new('RGB', (W, H + 150), 'white')
    canvas.paste(im, (0, 0))
    dr = ImageDraw.Draw(canvas)
    for a, b in zip(seq, seq[1:]):
        _, par = fields[a]
        for p, q in zip(*(lambda t: (t, t[1:]))(trace(par, w, cells[b]))):
            dr.line([p, q], fill='#1a73e8', width=5)

    def badge(cell, label, bg, fg='white', r=13):
        x, y = cell[0] * F + F // 2, cell[1] * F + F // 2
        dr.ellipse([x - r, y - r, x + r, y + r], fill=bg, outline='white', width=2)
        tw = dr.textlength(label, font=fnt_b)
        dr.text((x - tw / 2, y - 10), label, fill=fg, font=fnt_b)

    try:
        fnt_b = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 16)
        fnt = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 19)
        fnt_t = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 24)
    except OSError:
        fnt_b = fnt = fnt_t = ImageFont.load_default()

    badge(cells['ENTRANCE'], 'S', '#188038', r=15)
    for i, s in enumerate(stop_names, 1):
        badge(cells[s], str(i), '#d93025')
    badge(cells['CHECKOUT'], '$', '#188038', r=15)

    dr.text((30, H + 12), 'Optimal route  ·  entrance -> 11 items -> checkout',
            fill='#111', font=fnt_t)
    legend = '   '.join(f'{i}. {next(it for it, loc in ITEMS if loc == s)} ({s.title()})'
                        for i, s in enumerate(stop_names, 1))
    half = len(stop_names) // 2 + 1
    parts = legend.split('   ')
    dr.text((30, H + 55), '   '.join(parts[:half]), fill='#333', font=fnt)
    dr.text((30, H + 88), '   '.join(parts[half:]), fill='#333', font=fnt)
    dr.text((30, H + 121),
            f'Walking distance ~{best_cells * to_m:.0f} m '
            f'(vs {naive_cells * to_m:.0f} m shopping the list in written order '
            f'-> {100 * (1 - best_cells / naive_cells):.0f}% shorter). '
            f'Scale assumes ~3 m aisle pitch.',
            fill='#666', font=fnt)

    canvas.save(OUT_PATH)
    print(f'\nsaved {OUT_PATH}')


if __name__ == '__main__':
    main()
