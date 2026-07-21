"""Ground-truth walkability tests for the #659 map layer.

Coordinates are PDF points on page 1 of guide-austin-659.pdf, picked off
data/qa/walkable_overlay.png (regenerate with map_qa.py). If geometry or
exclusions change, rebuild first:
    python3 extract_659.py && python3 build_profile.py && python3 map_qa.py
"""
import json
import numpy as np
import pytest
from scipy import ndimage
from router import engine

CELL = 4.0
_p = np.load("data/heb659_profile.npz", allow_pickle=True)
FREE = _p["free"]
GEOM = json.load(open("data/heb659_geometry.json"))


def walkable(x, y):
    return bool(FREE[int(y // CELL), int(x // CELL)])


MUST_NOT = [
    (150, 150, "parking lot NW"),
    (150, 400, "curbside pickup lanes"),
    (230, 700, "drive-thru approach"),
    (5, 5, "page corner NW"),
    (1261, 5, "page corner NE"),
    (5, 829, "page corner SW"),
    (1261, 829, "page corner SE"),
    (800, 80, "behind Dairy cases (exclusion)"),
    (400, 80, "top-left wall service gap (exclusion)"),
    (519, 660, "Lease room interior (enclosed)"),
    (760, 700, "Texas Backyard interior (enclosed)"),
]
MUST = [
    (652, 568, "front action alley south of checkstands"),
    (688, 545, "checkout lane between checkstands"),
    (800, 130, "corridor in front of Dairy"),
    (946, 485, "Produce area"),
    (350, 122, "aisle 27 corridor, left wing"),
]


@pytest.mark.parametrize("x,y,where", MUST_NOT, ids=[m[2] for m in MUST_NOT])
def test_not_walkable(x, y, where):
    assert not walkable(x, y), f"{where} must NOT be walkable"


@pytest.mark.parametrize("x,y,where", MUST, ids=[m[2] for m in MUST])
def test_walkable(x, y, where):
    assert walkable(x, y), f"{where} must be walkable"


def test_walkable_is_inside_boundary():
    bmask = engine.build_grid({"page": GEOM["page"],
                               "boundary": GEOM["boundary"],
                               "fixtures": [], "obstacle_paths": []})
    assert not (FREE & ~bmask).any(), "walkable cells exist outside the store"


def test_single_connected_component():
    _, n = ndimage.label(FREE)
    assert n == 1, f"walkable region split into {n} components"


def test_walkable_fraction_sane():
    # catches both "sealed store" and "the world is walkable" regressions
    assert 0.10 < FREE.mean() < 0.40, FREE.mean()


def test_all_anchor_pairs_connected():
    assert (_p["D"] >= 0).all()
