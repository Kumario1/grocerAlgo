#!/usr/bin/env python3
"""Map-layer QA harness: visual + numeric diagnostics for the walkable grid.

Usage: python3 map_qa.py [store]   (default 659; reads data/<store>/,
writes data/<store>/qa/)

The iterate-to-perfect loop:
    python3 map_qa.py <store>    -> inspect data/<store>/qa/*.png + stats
    edit data/<store>/exclusions.json (rects the drawing shows open but
    shoppers can't use), rerun, then: python3 build_profile.py <store> && pytest

Outputs (data/<store>/qa/):
    walkable_overlay.png  green = walkable+reachable, purple = staff service
                          areas (sealed pockets holding a DELI/BAKERY/...
                          label), orange = walkable but cut off from the
                          entrance, red = the OLD no-boundary rule called it
                          walkable; sealed no-label crevices between fixtures
                          stay untinted. Dashed circles = VERIFY labels.
    reachable.png         entrance-connected region + anchors and their
                          snapped cells (red tie-line = snap moved far)
    corridor_width.png    distance-transform heat: dark red = sliver corridors
                          (exclusion candidates), green = comfortably wide
    report.json           every printed diagnostic, machine-readable — the
                          headless onboarding agent's interface
"""
import json, os, sys
import numpy as np
import fitz
from PIL import Image, ImageDraw
from scipy import ndimage
from router import engine, derive, qa_checks, raster

CELL = engine.CELL
STORE = sys.argv[1] if len(sys.argv) > 1 else "659"
DIR = f"data/{STORE}"
PDF = derive.pdf_path(STORE)
OUTDIR = f"{DIR}/qa"

# service departments where staff operate behind counters; if the area
# around one of these labels is still walkable, a human must check it
SERVICE_DEPTS = engine.SERVICE_DEPTS

os.makedirs(OUTDIR, exist_ok=True)
cfg = derive.load_store(DIR)
geom, anchors, excl = cfg["geom"], cfg["anchors"], cfg["exclusions"]

# the one shared build path (router/derive.py) — identical to build_profile;
# QA renders from the UNCUT grid so isolated pockets show orange
built = derive.build_free(cfg)
free = built["free_uncut"]
free_raw, reach = built["free_raw"], built["reach"]
culled_pockets, staff_mask = built["culled"], built["staff_mask"]
incl_mask = built["incl_mask"]
h, w = free.shape
free_old = engine.build_grid({**geom, "boundary": None})
reachable = (reach >= 0).reshape(h, w)

try:
    m_per_cell = float(np.load(f"{DIR}/profile.npz",
                               allow_pickle=True)["m_per_cell"])
except Exception:
    m_per_cell = 0.1183 * CELL          # ~0.473 at 4-pt, scales with resolution

page = fitz.open(PDF)[1]
base = raster.render_source(page, geom, dpi=144)
S = base.width / geom["page"]["w"]          # pdf-pt -> render px


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
    rad = max(1, round(12 / CELL))                       # ~1.4 m service-label window
    disc = reachable[max(0, cy - rad):cy + rad + 1, max(0, cx - rad):cx + rad + 1]
    frac = float(disc.mean()) if disc.size else 0.0
    if frac > 0.4:
        verify.append((name, ax, ay, frac))

# --- 1. walkable_overlay.png ---
# purple is ONLY label-identified staff areas; sealed crevices between
# fixtures (lead nowhere, not staff sections) stay untinted like furniture
im = tint(base, free_old & ~free_raw, (220, 30, 30))      # reclaimed outside
im = tint(im, staff_mask, (150, 60, 200))                 # staff service areas
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
    far_thresh = round(24 / CELL)                        # ~2.8 m far-snap flag
    color = "red" if moved > far_thresh else "blue"
    if moved > far_thresh:
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
cap = ndimage.maximum_filter(d, size=2 * max(1, round(8 / CELL)) + 1)  # ~1 m radius
v = np.clip(cap / (1.2 / m_per_cell), 0, 1)   # green saturates at ~2.4 m full width
rgb = np.zeros((h, w, 3), np.uint8)
rgb[..., 0] = (255 * (1 - v)).astype(np.uint8)
rgb[..., 1] = (255 * v).astype(np.uint8)
heat = Image.fromarray(rgb).resize(base.size, Image.NEAREST)
mm = Image.fromarray(reachable.astype(np.uint8) * 255).resize(base.size,
                                                              Image.NEAREST)
Image.composite(Image.blend(base, heat, 0.6), base, mm) \
     .save(f"{OUTDIR}/corridor_width.png")

# --- coverage nets (router/qa_checks.py): missed-section detectors that are
# independent of any authored truth — the onboarding agent cannot pass its
# own blind spots through these ---
cov = qa_checks.coverage(raster.coverage_words(page, geom), base, cfg, built,
                         m_per_cell)
label_clusters = cov["unreachable_shelf_labels"]
floor_patches = cov["sealed_floor_patches"]

# draw coverage findings on the overlay so the agent SEES them: red X per
# label cluster, red box per sealed floor patch
im = Image.open(f"{OUTDIR}/walkable_overlay.png")
dr = ImageDraw.Draw(im)
for c in label_clusters:
    x, y, r = c["x"] * S, c["y"] * S, 18 * S
    dr.line([x - r, y - r, x + r, y + r], fill="red", width=6)
    dr.line([x - r, y + r, x + r, y - r], fill="red", width=6)
for p in floor_patches:
    x, y, r = p["x"] * S, p["y"] * S, 14 * S
    dr.rectangle([x - r, y - r, x + r, y + r], outline="red", width=5)
if label_clusters or floor_patches:
    im.save(f"{OUTDIR}/walkable_overlay.png")

# --- stats ---
labels, ncomp = ndimage.label(free)
sizes = sorted(np.bincount(labels.ravel())[1:], reverse=True)
print(f"walkable: {free.mean() * 100:.1f}% of page "
      f"({reachable.mean() * 100:.1f}% entrance-reachable)")
print(f"components: {ncomp}  sizes: {sizes[:5]}{'...' if ncomp > 5 else ''}")
print(f"anchors: {len(anchors)}  |  exclusions: {len(excl)}")
if culled_pockets:
    print(f"service pockets sealed (service + dead crevices): {len(culled_pockets)}")
    for size, px, py in sorted(culled_pockets, reverse=True)[:10]:
        near = min(anchors, key=lambda a: (anchors[a][0] - px) ** 2
                   + (anchors[a][1] - py) ** 2)
        print(f"  {size:5d} cells at ({px:.0f},{py:.0f}) near {near}")
for name, ax, ay, frac in verify:
    print(f"VERIFY: {name} area still {frac * 100:.0f}% walkable at "
          f"({ax:.0f},{ay:.0f}) — staff area? add an exclusion rect if so")
if far_snaps:
    print(f"snaps moved >{round(24 / CELL)} cells (check reachable.png):")
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
for c in label_clusters:
    print(f"COVERAGE: {c['n']} shelf labels with no reachable frontage near "
          f"({c['x']:.0f},{c['y']:.0f}) — {', '.join(c['labels'])} — missed "
          f"section? open it (inclusion) or bless it (exclusion)")
for p in floor_patches:
    print(f"COVERAGE: {p['m2']:.0f} m^2 of painted floor sealed at "
          f"({p['x']:.0f},{p['y']:.0f}) — missed section? open it (inclusion) "
          f"or bless it (exclusion)")

# --- machine-readable report (the headless-agent loop reads this; same
# numbers as the prints above, deterministically ordered) ---
report = {
    "store": STORE,
    "walkable_pct": round(float(free.mean()) * 100, 1),
    "reachable_pct": round(float(reachable.mean()) * 100, 1),
    "m_per_cell": round(m_per_cell, 4),
    "components": {"n": int(ncomp), "sizes": [int(s) for s in sizes]},
    "culled_pockets": [
        {"cells": int(size), "x": round(px, 1), "y": round(py, 1),
         "near": min(anchors, key=lambda a: (anchors[a][0] - px) ** 2
                     + (anchors[a][1] - py) ** 2)}
        for size, px, py in sorted(culled_pockets,
                                   key=lambda t: (-t[0], t[1], t[2]))],
    "verify": [
        {"name": name, "x": round(ax, 1), "y": round(ay, 1),
         "frac": round(frac, 3)}
        for name, ax, ay, frac in verify],           # already sorted by name
    "far_snaps": [
        {"name": name, "cells": round(moved, 1),
         "meters": round(moved * m_per_cell, 2)}
        for name, moved in sorted(far_snaps, key=lambda t: (-t[1], t[0]))],
    "narrow": [
        {"name": name, "half_width_m": round(hw_cells * m_per_cell, 2)}
        for hw_cells, name in narrow[:8]],
    "coverage": cov,
    "provenance": cfg["provenance"],
}
with open(f"{OUTDIR}/report.json", "w") as f:
    json.dump(report, f, indent=1, sort_keys=True)
    f.write("\n")
