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

from router import derive, qa_checks

STORES = sorted(
    os.path.basename(os.path.dirname(p))
    for p in glob.glob("data/*/walk_truth.json")
    if os.path.exists(os.path.join(os.path.dirname(p), "profile.npz")))


@pytest.mark.parametrize("store", STORES)
def test_no_missed_sections(store):
    cfg = derive.load_store(f"data/{store}")
    built = derive.build_free(cfg)
    page = fitz.open(derive.pdf_path(store))[1]
    pix = page.get_pixmap(dpi=144)
    base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    m = float(np.load(f"data/{store}/profile.npz",
                      allow_pickle=True)["m_per_cell"])
    cov = qa_checks.coverage(page.get_text("words"), base, cfg, built, m)
    assert cov["unreachable_shelf_labels"] == [], \
        f"shelf labels with no reachable frontage: {cov['unreachable_shelf_labels']}"
    assert cov["sealed_floor_patches"] == [], \
        f"sealed painted-floor patches: {cov['sealed_floor_patches']}"


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
