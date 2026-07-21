"""Routing engine: occupancy grid, BFS distances, fixed-endpoint TSP."""
import numpy as np
from collections import deque

def build_grid(geom, cell=4.0):
    """True = walkable. Fixtures block their interior; walls block their line."""
    w = int(np.ceil(geom["page"]["w"] / cell))
    h = int(np.ceil(geom["page"]["h"] / cell))
    free = np.ones((h, w), bool)
    for x0, y0, x1, y1 in geom["fixtures"]:
        free[int(y0 // cell):int(np.ceil(y1 / cell)),
             int(x0 // cell):int(np.ceil(x1 / cell))] = False
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
    """Exact <=18 stops; nearest-neighbor + 2-opt beyond (plan.md §8.5)."""
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

def trace(parent, w, end_cell, cell=4.0):
    """Walk parents back from end_cell; return PDF-point path start->end."""
    path, u = [], end_cell[1] * w + end_cell[0]
    while u != -1:
        path.append((u % w * cell + cell / 2, u // w * cell + cell / 2))
        u = parent[u]
    return path[::-1]
