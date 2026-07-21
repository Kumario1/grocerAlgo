#!/usr/bin/env python3
"""geometry.json + zones.json -> profile.npz (grid, anchors, all-pairs D).

Usage: python3 build_profile.py [store]   (default 659; reads/writes data/<store>/)
"""
import sys, numpy as np
from router import engine, derive

STORE = sys.argv[1] if len(sys.argv) > 1 else "659"
DIR = f"data/{STORE}"

cfg = derive.load_store(DIR)
anchors = cfg["anchors"]
assert "ENTRANCE" in anchors and "CHECKOUT" in anchors, anchors.keys()

# the one shared build path (router/derive.py): exclusions -> staff-gap
# sealing (badge-protected, service-label condemned) -> inclusions ->
# entrance-seeded reachability -> cut to the entrance component
built = derive.build_free(cfg)
free, reach, culled = built["free"], built["reach"], built["culled"]
h, w = free.shape
if culled:
    print(f"service pockets culled: {len(culled)} "
          f"(largest {max(s for s, _, _ in culled)} cells)")

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
m_per_cell = 3.0 / (pitch_pts / engine.CELL)   # ponytail: coarse scale; label-only accuracy

np.savez_compressed(f"{DIR}/profile.npz", free=free, cell=engine.CELL,
                    names=names, cells=cells, D=D, parents=parents,
                    m_per_cell=m_per_cell)
print(f"{n} anchors, grid {w}x{h}, m/cell={m_per_cell:.3f} -> {DIR}/profile.npz")
