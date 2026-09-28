"""Coverage gates: no store ships with a missed section again.

Recomputes the router/qa_checks.py nets from source (PDF + config + grid)
for every onboarded store — independent of walk_truth.json, so an
onboarding agent cannot pass its own blind spots through these (store 24's
pharmacy wing shipped sealed on 2026-07-21 with the whole suite green;
these gates are the answer). Also asserts the committed qa/report.json is
converged and matches the shipped profile.
"""
import glob
import json
import os

import fitz
import numpy as np
import pytest
from PIL import Image

from router import derive, qa_checks, raster

STORES = sorted(
    os.path.basename(os.path.dirname(p))
    for p in glob.glob("data/*/walk_truth.json")
    if os.path.exists(os.path.join(os.path.dirname(p), "profile.npz")))


@pytest.mark.parametrize("store", STORES)
def test_no_missed_sections(store):
    cfg = derive.load_store(f"data/{store}")
    built = derive.build_free(cfg)
    page = fitz.open(derive.pdf_path(store))[1]
    base = raster.render_source(page, cfg["geom"], dpi=144)
    m = float(np.load(f"data/{store}/profile.npz",
                      allow_pickle=True)["m_per_cell"])
    cov = qa_checks.coverage(raster.coverage_words(page, cfg["geom"]), base,
                             cfg, built, m)
    assert cov["unreachable_shelf_labels"] == [], \
        f"shelf labels with no reachable frontage: {cov['unreachable_shelf_labels']}"
    assert cov["sealed_floor_patches"] == [], \
        f"sealed painted-floor patches: {cov['sealed_floor_patches']}"


def test_the_audit_worklist_catches_a_sealed_corridor():
    """qa_checks.suspects is advisory, so nothing else fails when it goes
    blind. Seal one of 659's aisle corridors and it must name the area."""
    cfg = derive.load_store("data/659")
    built = derive.build_free(cfg)
    page = fitz.open(derive.pdf_path("659"))[1]
    words = raster.coverage_words(page, cfg["geom"])
    m = float(np.load("data/659/profile.npz", allow_pickle=True)["m_per_cell"])
    before = qa_checks.suspects(words, cfg, built, m)

    # the store-24 failure mode: a corridor that is reachable at its badge
    # mouth and sealed deeper in. AISLE 20's badge, 30 pt of corridor cut.
    bx, by = cfg["anchors"]["AISLE 20"]
    reach = built["reach"].reshape(built["free"].shape).copy()
    cell = qa_checks.engine.CELL
    cx, cy = int(bx // cell), int(by // cell)
    reach[cy:cy + round(30 / cell), cx - 2:cx + 3] = -1
    sealed = qa_checks.suspects(words, cfg, {**built, "reach": reach.ravel()}, m)

    grew = [c for c in sealed["sealed_clusters"] if c not in
            before["sealed_clusters"] and abs(c["x"] - bx) < 40]
    assert grew, "a sealed aisle corridor produced no worklist entry"
    assert sealed["sealed_clusters_total"] > before["sealed_clusters_total"]


@pytest.mark.parametrize("store", STORES)
def test_report_converged(store):
    r = json.load(open(f"data/{store}/qa/report.json"))
    assert r["verify"] == [], "unresolved VERIFY flags in committed report"
    assert r["coverage"] == {"sealed_floor_patches": [],
                             "unreachable_shelf_labels": []}
    # the committed report must describe the shipped grid (staleness guard);
    # reachable_pct == the entrance-cut grid the profile stores
    free = np.load(f"data/{store}/profile.npz", allow_pickle=True)["free"]
    assert abs(r["reachable_pct"] - free.mean() * 100) < 0.1
