#!/usr/bin/env python3
"""Map-layer QA harness: visual + numeric diagnostics for the walkable grid.

The iterate-to-perfect loop:
    python3 map_qa.py            -> inspect data/qa/*.png + stdout stats
    edit data/heb659_exclusions.json (rects the drawing shows open but
    shoppers can't use), rerun, then: python3 build_profile.py && pytest

Outputs (data/qa/):
    walkable_overlay.png  green = walkable+reachable, orange = walkable but
                          cut off from the entrance, red = the OLD no-boundary
                          rule called it walkable (reclaimed outside space)
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
GEOM_PATH = sys.argv[1] if len(sys.argv) > 1 else "data/heb659_geometry.json"
PDF = sys.argv[2] if len(sys.argv) > 2 else "guide-austin-659.pdf"
OUTDIR = sys.argv[3] if len(sys.argv) > 3 else "data/qa"
PREFIX = GEOM_PATH.replace("_geometry.json", "")


def _load(path, default):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return default


os.makedirs(OUTDIR, exist_ok=True)
geom = json.load(open(GEOM_PATH))
zones = _load(PREFIX + "_zones.json", {})
excl = _load(PREFIX + "_exclusions.json", [])
anchors = {**geom["anchors"], **{k.upper(): v for k, v in zones.items()}}

free = engine.build_grid(geom, exclusions=[e["rect"] for e in excl])
free_old = engine.build_grid({**geom, "boundary": None})
h, w = free.shape

if "ENTRANCE" in anchors:
    seed_pt = anchors["ENTRANCE"]
else:  # no zones authored yet: seed from the aisle-badge centroid (in-store)
    ax = [v for k, v in anchors.items() if k.startswith("AISLE")]
    seed_pt = (sum(x for x, _ in ax) / len(ax), sum(y for _, y in ax) / len(ax))
    print("note: no ENTRANCE anchor — seeding reachability from aisle centroid")
seed = engine.nearest_free(free, seed_pt)
reach, _ = engine.bfs(free, seed)
reachable = (reach >= 0).reshape(h, w)

try:
    m_per_cell = float(np.load(PREFIX + "_profile.npz",
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


# --- 1. walkable_overlay.png ---
im = tint(base, free_old & ~free, (220, 30, 30))          # reclaimed
im = tint(im, free & ~reachable, (255, 140, 0))           # isolated pockets
im = tint(im, reachable, (30, 160, 60))                   # true walkable
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
d = ndimage.distance_transform_edt(reachable)
v = np.clip(d / 6.0, 0, 1)                 # 6 cells half-width ~ 2.8 m wide
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
if far_snaps:
    print("snaps moved >6 cells (check reachable.png):")
    for name, moved in sorted(far_snaps, key=lambda t: -t[1]):
        print(f"  {name}: {moved:.0f} cells (~{moved * m_per_cell:.1f} m)")
narrow = []
for name in sorted(anchors):
    sx, sy = engine.snap(free, reach, anchors[name])
    narrow.append((d[sy, sx], name))
narrow.sort()
print("narrowest corridors at anchors (half-width):")
for hw_cells, name in narrow[:8]:
    print(f"  {name}: {hw_cells * m_per_cell:.2f} m")
