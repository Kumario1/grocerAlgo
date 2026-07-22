"""Mechanical coverage nets: catch whole missed sections that a
converged-looking store can still hide.

Born 2026-07-21: store 24 shipped "converged" (zero VERIFY flags, full test
suite green) with its entire pharmacy wing sealed — the onboarding agent
authors walk_truth.json itself, so its blind spots pass its own tests.
These checks are independent of any authored truth.

Net 1 — shelf_label_coverage: every product label printed on/beside a shelf
fixture (Cotton Balls, Baby Wipes, ...) must have entrance-reachable floor
within FRONTAGE_R: shoppers must be able to stand in front of anything
that's for sale. Failing labels cluster; >= CLUSTER_MIN together is a
missed section, not decoration noise.

Net 2 — floor_paint_coverage: the map paints sales floor in one dominant
color (self-calibrated from the colors under KNOWN-reachable cells). A
large connected patch of floor paint that is unreachable is suspicious —
but only when OUR RULES sealed it and the paint holds evidence of shopping:
    - raw-unreachable regions (enclosed rooms drawn sealed: lease, Texas
      Backyard, restrooms) are exempt — no rule of ours made them;
    - rect-type seal zones (checkstand banks) are exempt — lanes are
      painted floor sealed BY DESIGN;
    - remaining patches flag only when corroborated: they contain failing
      shelf labels from net 1 or an aisle-badge point. Uncorroborated
      sealed paint is a staff crevice (659's pharmacy south band), the
      audit agent's visual sweep owns those.

Blessed-truth exemptions (both nets): staff_mask pockets (label-condemned)
and exclusion shapes (human-marked staff) are intentional seals, never
flags.

Constants are universal, calibrated so blessed store 659 reports ZERO flags
(it is the pixel-frozen baseline; its map is correct by definition). Never
tune them per store — a store that trips a net either has a real coverage
hole (fix its data) or reveals a cross-store pattern (universal change,
gated by the golden + every store's tests).
"""
import numpy as np
from PIL import Image
from scipy import ndimage

from router import engine

FRONTAGE_R = 16      # pt (~1.9 m) — max label-to-reachable-floor distance
NEAR_FIX = 4         # cells (~0.9 m) — label-to-shelf proximity for eligibility
CLUSTER_R = 40       # pt — failing labels this close merge into one finding
CLUSTER_MIN = 3      # labels — smaller groups are legend/decoration noise
PAINT_TOL = 60       # L1 |RGB - floor| classifier tolerance
PATCH_MIN_M2 = 25.0  # smaller sealed paint = crevice noise
CORROB_R = 6         # cells (~1.4 m) — failing-label-to-patch corroboration


def coverage(words, base_img, cfg, built, m_per_cell, cell=engine.CELL):
    """Run both nets. Returns {"unreachable_shelf_labels": [...],
    "sealed_floor_patches": [...]} (both [] on a fully covered store)."""
    geom = cfg["geom"]
    shape = built["free"].shape
    h, w = shape
    bound = engine.build_grid({"page": geom["page"],
                               "boundary": geom["boundary"],
                               "fixtures": [], "obstacle_paths": []}, cell)
    excl_mask = engine.shape_mask(cfg["exclusions"], shape, cell)
    blessed = built["staff_mask"] | excl_mask
    reachable = (built["reach"] >= 0).reshape(shape)
    dist_pt = ndimage.distance_transform_edt(~reachable) * cell
    # raw reachability: what the drawing alone (pre-sealing) connects to the
    # entrance. Enclosed rooms fail this — our rules never touched them.
    free_raw = built["free_raw"]
    raw_seed = engine.nearest_free(free_raw, built["seed_pt"], cell)
    raw_reach, _ = engine.bfs(free_raw, raw_seed)
    raw_reachable = (raw_reach >= 0).reshape(shape)

    # --- net 1: shelf-label frontage ---
    near_fixture = ndimage.binary_dilation(
        engine.shape_mask(
            [{"rect": list(f)} for f in geom["fixtures"]]
            + [{"poly": p} for p in geom.get("fixture_polys") or []],
            shape, cell),
        iterations=NEAR_FIX)
    fails = []
    for x0, y0, x1, y1, t, *_ in words:
        if t.isdigit() or len(t) < 2:        # badges / stray glyphs
            continue
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        cx, cy = int(mx // cell), int(my // cell)
        if not (0 <= cy < h and 0 <= cx < w):
            continue
        if (not bound[cy, cx] or not near_fixture[cy, cx]
                or not raw_reachable[cy, cx] or blessed[cy, cx]):
            continue
        if dist_pt[cy, cx] > FRONTAGE_R:
            fails.append((mx, my, t))

    clusters, used = [], [False] * len(fails)
    for i in range(len(fails)):
        if used[i]:
            continue
        group, used[i], grew = [i], True, True
        while grew:
            grew = False
            for j, (jx, jy, _) in enumerate(fails):
                if not used[j] and any(
                        (fails[k][0] - jx) ** 2 + (fails[k][1] - jy) ** 2
                        <= CLUSTER_R ** 2 for k in group):
                    group.append(j)
                    used[j] = grew = True
        if len(group) >= CLUSTER_MIN:
            xs = [fails[k][0] for k in group]
            ys = [fails[k][1] for k in group]
            clusters.append({"n": len(group),
                             "x": round(sum(xs) / len(xs), 1),
                             "y": round(sum(ys) / len(ys), 1),
                             "labels": sorted({fails[k][2] for k in group})[:6]})
    clusters.sort(key=lambda c: (-c["n"], c["x"], c["y"]))

    # --- net 2: sealed floor paint, corroborated ---
    arr = np.asarray(base_img.resize((w, h), Image.BILINEAR), np.int16)
    floor = np.median(arr[reachable], axis=0)      # dominant sales-floor RGB
    painted = np.abs(arr - floor).sum(axis=2) < PAINT_TOL
    lane_zones = engine.shape_mask(
        [z for z in cfg["seal_zones"] if "rect" in z], shape, cell)
    missed = (painted & ~reachable & bound & raw_reachable
              & ~blessed & ~lane_zones)
    # heal the holes text glyphs punch in the paint before sizing regions
    missed = ndimage.binary_closing(missed, np.ones((3, 3), bool))
    fail_cells = np.zeros(shape, bool)
    for mx, my, _ in fails:
        fail_cells[int(my // cell), int(mx // cell)] = True
    badge_cells = np.zeros(shape, bool)
    for k, (bx, by) in cfg["anchors"].items():
        if k.startswith("AISLE "):
            badge_cells[int(by // cell), int(bx // cell)] = True
    labels_, n = ndimage.label(missed)
    patches = []
    for i in range(1, n + 1):
        m = labels_ == i
        m2 = float(m.sum()) * m_per_cell ** 2
        if m2 < PATCH_MIN_M2:
            continue
        wide = ndimage.binary_dilation(m, iterations=CORROB_R)
        if not (wide & fail_cells).any() and not (m & badge_cells).any():
            continue                             # uncorroborated staff crevice
        ys, xs = np.where(m)
        patches.append({"m2": round(m2, 1),
                        "x": round(float(xs.mean()) * cell, 1),
                        "y": round(float(ys.mean()) * cell, 1)})
    patches.sort(key=lambda p: (-p["m2"], p["x"], p["y"]))

    return {"unreachable_shelf_labels": clusters,
            "sealed_floor_patches": patches}
