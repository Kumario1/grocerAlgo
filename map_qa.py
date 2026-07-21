#!/usr/bin/env python3
"""Map-layer QA harness: visual + numeric diagnostics for the walkable grid.

Usage: python3 map_qa.py [store]   (default 659; reads data/<store>/,
writes data/<store>/qa/)

The iterate-to-perfect loop:
    python3 map_qa.py <store>    -> inspect data/<store>/qa/*.png + stats
    edit data/<store>/exclusions.json (rects the drawing shows open but
    shoppers can't use), rerun, then: python3 build_profile.py <store> && pytest

Outputs (data/<store>/qa/):
    walkable_overlay.png  green = walkable+reachable, purple = staff-gap-
                          sealed service interiors, orange = walkable but
                          cut off from the entrance, red = the OLD
                          no-boundary rule called it walkable. Dashed
                          circles = VERIFY service labels (see stats).
    reachable.png         entrance-connected region + anchors and their
                          snapped cells (red tie-line = snap moved far)
    corridor_width.png    distance-transform heat: dark red = sliver corridors
                          (exclusion candidates), green = comfortably wide
"""
import json, os, sys
import numpy as np
import fitz
from PIL import Image, ImageDraw
from scipy import ndimage
from router import engine

CELL = 4.0
STORE = sys.argv[1] if len(sys.argv) > 1 else "659"
DIR = f"data/{STORE}"
PDF = f"guide-austin-{STORE}.pdf"
OUTDIR = f"{DIR}/qa"

# service departments where staff operate behind counters; if the area
# around one of these labels is still walkable, a human must check it
SERVICE_DEPTS = ("DELI", "BAKERY", "SEAFOOD", "SUSHI", "KITCHEN",
                 "PHARMACY", "MEAL SIMPLE", "COOKING")


def _load(path, default):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return default


os.makedirs(OUTDIR, exist_ok=True)
geom = json.load(open(f"{DIR}/geometry.json"))
zones = _load(f"{DIR}/zones.json", {})
excl = _load(f"{DIR}/exclusions.json", [])
incl = _load(f"{DIR}/inclusions.json", [])
anchors = {**geom["anchors"], **{k.upper(): v for k, v in zones.items()}}

free_raw = engine.build_grid(geom, exclusions=excl)
free_old = engine.build_grid({**geom, "boundary": None})
h, w = free_raw.shape

if "ENTRANCE" in anchors:
    seed_pt = anchors["ENTRANCE"]
else:  # no zones authored yet: seed from the aisle-badge centroid (in-store)
    ax = [v for k, v in anchors.items() if k.startswith("AISLE")]
    seed_pt = (sum(x for x, _ in ax) / len(ax), sum(y for _, y in ax) / len(ax))
    print("note: no ENTRANCE anchor — seeding reachability from aisle centroid")

badges = [v for k, v in anchors.items() if k.startswith("AISLE ")]
free, culled_pockets = engine.seal_staff_gaps(free_raw, seed_pt,
                                              protect_pts=badges)
incl_mask = engine.shape_mask(incl, free.shape)
free |= free_raw & incl_mask          # human-verified customer zones win
seed = engine.nearest_free(free, seed_pt)
reach, _ = engine.bfs(free, seed)
reachable = (reach >= 0).reshape(h, w)

try:
    m_per_cell = float(np.load(f"{DIR}/profile.npz",
                               allow_pickle=True)["m_per_cell"])
except Exception:
    m_per_cell = 0.473

page = fitz.open(PDF)[1]
pix = page.get_pixmap(dpi=144)
base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
S = pix.width / geom["page"]["w"]          # pdf-pt -> render px


def tint(img, mask, color, alpha=0.45):
    mm = Image.fromarray(mask.astype(np.uint8) * 255).resize(img.size,
                                                             Image.NEAREST)
    return Image.composite(Image.blend(img, Image.new("RGB", img.size, color),
                                       alpha), img, mm)


# --- service-label attention pass (Rule 2): after all rules, is the area
# around a service-department label still walkable? Then a human must judge.
verify = []
for name in sorted(anchors):
    if not any(s in name for s in SERVICE_DEPTS):
        continue
    ax, ay = anchors[name]
    cx, cy = int(ax // CELL), int(ay // CELL)
    if not (0 <= cy < h and 0 <= cx < w) or not reachable[cy, cx]:
        continue  # the label itself is sealed/excluded -> area was handled
    if incl_mask[cy, cx]:
        continue  # inside a human-verified inclusion rect -> already judged
    disc = reachable[max(0, cy - 3):cy + 4, max(0, cx - 3):cx + 4]
    frac = float(disc.mean()) if disc.size else 0.0
    if frac > 0.4:
        verify.append((name, ax, ay, frac))

# --- 1. walkable_overlay.png ---
im = tint(base, free_old & ~free_raw, (220, 30, 30))      # reclaimed outside
im = tint(im, free_raw & ~free, (150, 60, 200))           # staff-gap sealed
im = tint(im, free & ~reachable, (255, 140, 0))           # isolated pockets
im = tint(im, reachable, (30, 160, 60))                   # true walkable
dr = ImageDraw.Draw(im)
for name, ax, ay, _ in verify:                            # dashed = manual
    x, y, r = ax * S, ay * S, 14 * S
    for a0 in range(0, 360, 30):
        dr.arc([x - r, y - r, x + r, y + r], a0, a0 + 15, fill="red", width=5)
im.save(f"{OUTDIR}/walkable_overlay.png")

# --- 2. reachable.png ---
im = tint(base, reachable, (30, 160, 60), 0.35)
dr = ImageDraw.Draw(im)
far_snaps = []
for name in sorted(anchors):
    ax, ay = anchors[name]
    sx, sy = engine.snap(free, reach, anchors[name])
    px, py = ax * S, ay * S
    qx, qy = (sx * CELL + CELL / 2) * S, (sy * CELL + CELL / 2) * S
    moved = max(abs(qx - px), abs(qy - py)) / (CELL * S)
    color = "red" if moved > 6 else "blue"
    if moved > 6:
        far_snaps.append((name, moved))
    dr.line([px, py, qx, qy], fill=color, width=2)
    dr.ellipse([px - 4, py - 4, px + 4, py + 4], fill=color)
    dr.ellipse([qx - 3, qy - 3, qx + 3, qy + 3], outline="black")
im.save(f"{OUTDIR}/reachable.png")

# --- 3. corridor_width.png ---
# Color = the corridor's CAPACITY (max wall-distance within 2 cells), not the
# cell's own wall-distance — otherwise every shelf-front cell reads d=1 and
# whole normal aisles paint red. Green saturates at a realistic store aisle
# (~2.4 m full width); red = genuinely tight (<~1.2 m), worth a human look.
d = ndimage.distance_transform_edt(reachable)
cap = ndimage.maximum_filter(d, size=5)
v = np.clip(cap / 2.5, 0, 1)               # 2.5 cells half-width ~ 2.4 m wide
rgb = np.zeros((h, w, 3), np.uint8)
rgb[..., 0] = (255 * (1 - v)).astype(np.uint8)
rgb[..., 1] = (255 * v).astype(np.uint8)
heat = Image.fromarray(rgb).resize(base.size, Image.NEAREST)
mm = Image.fromarray(reachable.astype(np.uint8) * 255).resize(base.size,
                                                              Image.NEAREST)
Image.composite(Image.blend(base, heat, 0.6), base, mm) \
     .save(f"{OUTDIR}/corridor_width.png")

# --- stats ---
labels, ncomp = ndimage.label(free)
sizes = sorted(np.bincount(labels.ravel())[1:], reverse=True)
print(f"walkable: {free.mean() * 100:.1f}% of page "
      f"({reachable.mean() * 100:.1f}% entrance-reachable)")
print(f"components: {ncomp}  sizes: {sizes[:5]}{'...' if ncomp > 5 else ''}")
print(f"anchors: {len(anchors)}  |  exclusions: {len(excl)}")
if culled_pockets:
    print(f"service pockets sealed (>=25 cells): {len(culled_pockets)}")
    for size, px, py in sorted(culled_pockets, reverse=True)[:10]:
        near = min(anchors, key=lambda a: (anchors[a][0] - px) ** 2
                   + (anchors[a][1] - py) ** 2)
        print(f"  {size:5d} cells at ({px:.0f},{py:.0f}) near {near}")
for name, ax, ay, frac in verify:
    print(f"VERIFY: {name} area still {frac * 100:.0f}% walkable at "
          f"({ax:.0f},{ay:.0f}) — staff area? add an exclusion rect if so")
if far_snaps:
    print("snaps moved >6 cells (check reachable.png):")
    for name, moved in sorted(far_snaps, key=lambda t: -t[1]):
        print(f"  {name}: {moved:.0f} cells (~{moved * m_per_cell:.1f} m)")
narrow = []
for name in sorted(anchors):
    sx, sy = engine.snap(free, reach, anchors[name])
    narrow.append((cap[sy, sx], name))     # corridor capacity, not distance to
narrow.sort()                              # the shelf the anchor snapped onto
print("tightest corridors at anchors (half-width capacity):")
for hw_cells, name in narrow[:8]:
    print(f"  {name}: {hw_cells * m_per_cell:.2f} m")
