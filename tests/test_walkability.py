"""Ground-truth walkability tests, parametrized over every onboarded store.

A store participates as soon as it has BOTH data/<store>/walk_truth.json
and data/<store>/profile.npz — no test edits, ever. Truth points are PDF
points on the map page, picked off data/<store>/qa/walkable_overlay.png
(regenerate with ./rebuild.sh <store>), schema:
    {"must": [[x, y, "why"], ...], "must_not": [[x, y, "why"], ...]}
"""
import glob
import json
import os

import numpy as np
import pytest
from scipy import ndimage

from router import engine

STORES = sorted(
    os.path.basename(os.path.dirname(p))
    for p in glob.glob("data/*/walk_truth.json")
    if os.path.exists(os.path.join(os.path.dirname(p), "profile.npz")))
assert STORES, "no store has walk_truth.json + profile.npz"

_profiles = {}


def _store(store):
    if store not in _profiles:
        _profiles[store] = np.load(f"data/{store}/profile.npz",
                                   allow_pickle=True)
    return _profiles[store]


def _walkable(store, x, y):
    p = _store(store)
    cell = float(p["cell"])                    # resolution from the npz,
    return bool(p["free"][int(y // cell), int(x // cell)])  # never hardcoded

MUST, MUST_NOT = [], []
for _s in STORES:
    _t = json.load(open(f"data/{_s}/walk_truth.json"))
    MUST += [pytest.param(_s, x, y, w, id=f"{_s}:{w}")
             for x, y, w in _t["must"]]
    MUST_NOT += [pytest.param(_s, x, y, w, id=f"{_s}:{w}")
                 for x, y, w in _t["must_not"]]


@pytest.mark.parametrize("store,x,y,where", MUST_NOT)
def test_not_walkable(store, x, y, where):
    assert not _walkable(store, x, y), \
        f"store {store}: {where} must NOT be walkable"


@pytest.mark.parametrize("store,x,y,where", MUST)
def test_walkable(store, x, y, where):
    assert _walkable(store, x, y), f"store {store}: {where} must be walkable"


@pytest.mark.parametrize("store", STORES)
def test_walkable_is_inside_boundary(store):
    geom = json.load(open(f"data/{store}/geometry.json"))
    free = _store(store)["free"]
    bmask = engine.build_grid({"page": geom["page"],
                               "boundary": geom["boundary"],
                               "fixtures": [], "obstacle_paths": []},
                              cell=float(_store(store)["cell"]))
    assert not (free & ~bmask).any(), "walkable cells exist outside the store"


@pytest.mark.parametrize("store", STORES)
def test_single_connected_component(store):
    _, n = ndimage.label(_store(store)["free"])
    assert n == 1, f"walkable region split into {n} components"


@pytest.mark.parametrize("store", STORES)
def test_walkable_fraction_sane(store):
    # catches both "sealed store" and "the world is walkable" regressions
    frac = _store(store)["free"].mean()
    assert 0.10 < frac < 0.40, frac


@pytest.mark.parametrize("store", STORES)
def test_all_anchor_pairs_connected(store):
    assert (_store(store)["D"] >= 0).all()
