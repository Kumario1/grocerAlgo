"""Ground-truth walkability tests for the #659 map layer.

Coordinates are PDF points on page 1 of guide-austin-659.pdf, picked off
data/659/qa/walkable_overlay.png (regenerate with map_qa.py). If geometry or
exclusions change, rebuild first:
    python3 extract.py && python3 build_profile.py && python3 map_qa.py
"""
import json
import numpy as np
import pytest
from scipy import ndimage
from router import engine

_p = np.load("data/659/profile.npz", allow_pickle=True)
CELL = float(_p["cell"])
FREE = _p["free"]
GEOM = json.load(open("data/659/geometry.json"))


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
    # staff-only service areas per store-owner markup (2026-07-21 pictures)
    (851, 280, "Deli island interior (staff)"),
    (880, 400, "Bakery interior (staff)"),
    (833, 330, "wine-back service strip (staff gap)"),
    (314, 624, "Pharmacy back area, below shelf row (staff)"),
    (300, 532, "Pharmacy room behind counter wall (staff)"),
    (1060, 400, "Seafood counter (staff)"),
    (1090, 660, "Meal Simple prep block (staff)"),
    # sealed by the staff-gap rule; conservative — shoppers do exit through
    # lanes, but no stop/anchor ever needs to stand inside one
    (688, 545, "checkout lane between checkstands"),
    # rotated-quad cafe table by the island east walkway: exact-geometry
    # capture (2026-07-21); you can't walk through a table
    (990, 430, "cafe table (rotated quad fixture)"),
]
MUST = [
    (652, 568, "front action alley south of checkstands"),
    (800, 130, "corridor in front of Dairy"),
    (946, 485, "Produce area"),
    (350, 122, "aisle 27 corridor, left wing"),
    (808, 300, "aisle 15 corridor west of wine shelf"),
    # customer zones from owner green markup (inclusions.json)
    (332, 532, "Pharmacy pickup corridor, east of counter wall"),
    (370, 510, "aisle 42 west end by pharmacy"),
    # (990,430) turned out to BE a cafe table — a rotated quad the old
    # extractor dropped; the walkway beside the tables is the customer space
    (955, 430, "island east walkway beside cafe tables"),
    (884, 500, "Floral front"),
    (1100, 475, "Seafood south frontage"),
    # narrow aisles rescued by badge protection (owner markup round 3)
    (390, 397, "left-wing aisle 37 corridor"),
    (544, 450, "Toys aisle, south bank (badge 22)"),
    (781, 300, "north-bank corridor by Tortillas shelf"),
    (1110, 592, "Produce south walkway"),
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
