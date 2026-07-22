#!/usr/bin/env python3
"""geometry.json + per-store config (file or derived) -> profile.npz
(grid, anchors, all-pairs D).

Usage: python3 build_profile.py [store]   (default 659; reads/writes data/<store>/)
"""
import sys, numpy as np
from router import engine, derive

STORE = sys.argv[1] if len(sys.argv) > 1 else "659"
DIR = f"data/{STORE}"

cfg = derive.load_store(DIR)   # zones/seal_zones auto-derive when no file;
anchors = cfg["anchors"]       # hard-errors (actionable) if underivable

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

# calibration: aisle pitch ~ 3.0 m. Group adjacent parallel aisle labels by
# number-row; derive_aisle_pitch rejects stacked labels and block-sized gaps.
pitch_pts = derive.derive_aisle_pitch(anchors)
m_per_cell = 3.0 / (pitch_pts / engine.CELL)   # ponytail: coarse scale; label-only accuracy

np.savez_compressed(f"{DIR}/profile.npz", free=free, cell=engine.CELL,
                    names=names, cells=cells, D=D, parents=parents,
                    m_per_cell=m_per_cell)
print(f"{n} anchors, grid {w}x{h}, m/cell={m_per_cell:.3f} -> {DIR}/profile.npz")
