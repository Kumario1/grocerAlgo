#!/usr/bin/env python3
"""geometry.json + zones.json -> heb659_profile.npz (grid, anchors, all-pairs D)."""
import json, numpy as np
from router import engine

geom = json.load(open("data/heb659_geometry.json"))
zones = json.load(open("data/heb659_zones.json"))
anchors = {**geom["anchors"], **{k.upper(): v for k, v in zones.items()}}
assert "ENTRANCE" in anchors and "CHECKOUT" in anchors, anchors.keys()

free = engine.build_grid(geom)
h, w = free.shape

# reachability field from the entrance defines legal space for snapping
ent_cell = (int(anchors["ENTRANCE"][0] // 4), int(anchors["ENTRANCE"][1] // 4))
if not free[ent_cell[1], ent_cell[0]]:
    d0, _ = engine.bfs(free, (ent_cell[0], min(h - 1, ent_cell[1] + 2)))
else:
    d0, _ = engine.bfs(free, ent_cell)
reach = d0

names = sorted(anchors)
cells = np.array([engine.snap(free, reach, anchors[n]) for n in names])

# every anchor must be reachable from the entrance (plan.md §8.1 validation)
for n, (cx, cy) in zip(names, cells):
    assert reach[cy * w + cx] >= 0, f"anchor {n} unreachable from entrance"

n = len(names)
D = np.zeros((n, n), np.int32)
parents = np.zeros((n, h * w), np.int32)
for i, (cx, cy) in enumerate(cells):
    dist, par = engine.bfs(free, (cx, cy))
    parents[i] = par
    for j, (bx, by) in enumerate(cells):
        D[i, j] = dist[by * w + bx]
assert (D >= 0).all(), "disconnected anchor pair"

# calibration: aisle pitch ~ 3.0 m. Adjacent PARALLEL aisles live in the same
# number-row; a flat median over all aisle x is fooled by the ~14 left-wall
# aisle numbers stacked at one x (deviation from plan: that gave pitch 6.3pt ->
# an absurd 784 m corner-to-corner). Group by number-row, pool within-row gaps.
from collections import defaultdict
_rows = defaultdict(list)
for k in names:
    if k.startswith("AISLE"):
        _rows[round(anchors[k][1] / 25)].append(anchors[k][0])
_pitches = []
for _xs in _rows.values():
    _pitches += [d for d in np.diff(sorted(_xs)) if 10 < d < 35]  # skip stacks/block gaps
pitch_pts = float(np.median(_pitches))
m_per_cell = 3.0 / (pitch_pts / 4.0)   # ponytail: coarse scale; label-only accuracy

np.savez_compressed("data/heb659_profile.npz", free=free, cell=4.0,
                    names=names, cells=cells, D=D, parents=parents,
                    m_per_cell=m_per_cell)
print(f"{n} anchors, grid {w}x{h}, m/cell={m_per_cell:.3f} -> data/heb659_profile.npz")
