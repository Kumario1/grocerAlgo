"""Auto-derivation of per-store config (router/derive.py) on toy anchors —
no store data involved except the tmp_path file-precedence check."""
import json

import pytest

from router import derive


# --- derive_zones ---

def test_zones_from_labels():
    z = derive.derive_zones({"ENTRANCE": [890, 724], "CHECKSTANDS": [688, 677]})
    assert z == {"ENTRANCE": [890, 724], "CHECKOUT": [688, 677]}


def test_zones_prefix_fallbacks():
    z = derive.derive_zones({"ENTRANCE EXIT": [10, 20], "CHECK STANDS": [30, 40]})
    assert z == {"ENTRANCE": [10, 20], "CHECKOUT": [30, 40]}


def test_zones_missing_entrance_is_actionable_hard_error():
    with pytest.raises(SystemExit, match="ENTRANCE"):
        derive.derive_zones({"CHECKSTANDS": [1, 2]}, "data/999")
    with pytest.raises(SystemExit, match="zones.json"):
        derive.derive_zones({"AISLE 1": [1, 2]}, "data/999")


# --- derive_seal_zones: service disks ---

def test_deli_anchor_yields_disk():
    zones = derive.derive_seal_zones({"DELI": [648, 440]}, [])
    assert zones == [{"name": "auto:DELI", "pt": [648, 440],
                      "r": derive.DISK_R, "bridge": 12}]


def test_seafood_anchor_seals_wider_counter_entrances():
    zones = derive.derive_seal_zones({"SEAFOOD": [125, 252]}, [])
    assert zones == [{"name": "auto:SEAFOOD", "pt": [125, 252],
                      "r": derive.DISK_R, "bridge": 20}]


def test_noise_anchors_yield_nothing():
    # substring would wrongly match MEAT/FROZEN-adjacent noise; strict must not
    zones = derive.derive_seal_zones(
        {"CANNED MEAT": [709, 339], "POT PIE FROZEN DESSERTS": [326, 296],
         "MEAT": [100, 100], "DAIRY": [374, 193], "PRODUCE": [941, 638]}, [])
    assert zones == []


def test_blooms_merged_line_yields_floral_family_disk():
    # "BLOOMS RESTROOMS" = H-E-B floral brand merged with an adjacent label
    zones = derive.derive_seal_zones({"BLOOMS RESTROOMS": [733, 551]}, [])
    assert [z["name"] for z in zones] == ["auto:BLOOMS RESTROOMS"]
    assert zones[0]["pt"] == [733, 551]


# --- derive_seal_zones: checkstand rect ---

def test_checkstand_cluster_yields_padded_rect_bridge_20():
    fixtures = [[600, 660, 620, 690],       # in-window checkstand fixtures
                [640, 660, 660, 690],
                [700, 665, 720, 695],
                [100, 100, 120, 130],       # far away: not part of the bank
                [688, 300, 708, 330]]       # right x, wrong y
    zones = derive.derive_seal_zones({"CHECKSTANDS": [688, 677]}, fixtures)
    assert len(zones) == 1
    z = zones[0]
    assert z["name"] == "auto:CHECKSTANDS" and z["bridge"] == 20
    p = derive.CHECK_PAD
    assert z["rect"] == [600 - p, 660 - p, 720 + p, 695 + p]


def test_no_checkstands_label_no_rect():
    assert derive.derive_seal_zones({}, [[600, 660, 620, 690]]) == []


# --- load_store precedence ---

def _toy_store(tmp_path, **files):
    geom = {"page": {"w": 100.0, "h": 100.0},
            "anchors": {"ENTRANCE": [50, 90], "CHECKSTANDS": [50, 70],
                        "DELI": [20, 20]},
            "fixtures": [[40, 60, 60, 68]], "obstacle_paths": []}
    (tmp_path / "geometry.json").write_text(json.dumps(geom))
    for name, content in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(content))
    return tmp_path


def test_file_seal_zones_win_verbatim(tmp_path):
    mine = [{"name": "hand-tuned deli", "pt": [21, 21], "r": 55, "bridge": 9}]
    cfg = derive.load_store(str(_toy_store(tmp_path, seal_zones=mine)))
    assert cfg["seal_zones"] == mine                       # no merging
    assert cfg["provenance"]["seal_zones"] == "file"
    assert cfg["provenance"]["drift"]["seal_zones"]["derived_n"] == 2


def test_absent_files_derive_with_provenance(tmp_path):
    cfg = derive.load_store(str(_toy_store(tmp_path)))
    assert cfg["provenance"]["zones"] == "derived"
    assert cfg["provenance"]["seal_zones"] == "derived"
    assert cfg["provenance"]["exclusions"] == "absent"
    assert cfg["anchors"]["CHECKOUT"] == [50, 70]          # from CHECKSTANDS
    names = [z["name"] for z in cfg["seal_zones"]]
    assert names == ["auto:DELI", "auto:CHECKSTANDS"]
    assert cfg["provenance"]["drift"] is None


def test_file_zones_win_verbatim(tmp_path):
    cfg = derive.load_store(str(_toy_store(
        tmp_path, zones={"ENTRANCE": [51, 91], "CHECKOUT": [1, 2]})))
    assert cfg["anchors"]["ENTRANCE"] == [51, 91]
    assert cfg["anchors"]["CHECKOUT"] == [1, 2]
    assert cfg["provenance"]["zones"] == "file"
    assert "ENTRANCE" in cfg["provenance"]["drift"]["zones"]
