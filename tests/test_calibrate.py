"""The fitter must find #659's hand-derived answer on its own — and refuse
the failures that look like successes."""
import copy
import json

import numpy as np
import pytest

import calibrate as calibrate_cli
from router import calibrate as cal


@pytest.fixture(scope="module")
def atlas():
    return cal.load_atlas("659")


@pytest.fixture(scope="module")
def guide():
    with open("data/659/geometry.json") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def profile():
    return cal.load_profile("659")


def test_search_recovers_the_hand_derived_659_correspondence(atlas, guide):
    found = cal.fit(atlas["geometry"], guide)

    # x from the top row of aisles at the same numbers, y from the left-hand
    # column four apart — the correspondence a human had to find for #659.
    assert found["x"]["aisle_offset"] == 0
    assert found["y"]["aisle_offset"] == 4
    assert found["gates"] == {"residual": True, "margin": True,
                              "scale_skew": True}


def test_searched_transform_agrees_with_the_pinned_one(atlas, guide):
    searched = cal.transform(cal.fit(atlas["geometry"], guide))
    pinned = cal.transform(cal.fit(atlas["geometry"], guide,
                                   cal.store_config("659")["atlas_fit"]))

    moved = [np.hypot(*(np.array(searched(p)) - np.array(pinned(p))))
             for p in atlas["psas"].values()]

    assert max(moved) < 3.0            # under one cell, over 2677 real PSAs


def test_pinned_fit_reproduces_the_frozen_659_constants(atlas, guide):
    found = cal.fit(atlas["geometry"], guide,
                    cal.store_config("659")["atlas_fit"])

    assert found["x"]["scale"] == pytest.approx(0.98950405)
    assert found["x"]["offset"] == pytest.approx(254.6131588)
    assert found["y"]["scale"] == pytest.approx(0.9751628)
    assert found["y"]["offset"] == pytest.approx(65.95428215)


def test_a_rotated_atlas_is_not_fitted_as_if_it_were_upright(atlas, guide):
    turned = copy.deepcopy(atlas["geometry"])
    turned["page"] = {"w": turned["page"]["h"], "h": turned["page"]["w"]}
    turned["anchors"] = {name: [xy[1], xy[0]]
                         for name, xy in turned["anchors"].items()}

    found = cal.fit(turned, guide)

    # Either it reads the swap, or it refuses — never a confident upright fit.
    if "x" in found:
        assert found["x"]["source_axis"] == 1
        assert any("rotated" in note for note in found["notes"])


def test_scrambled_aisle_positions_are_refused(atlas, guide):
    noise = copy.deepcopy(atlas["geometry"])
    rng = np.random.default_rng(0)
    noise["anchors"] = {
        name: ([xy[0] + rng.uniform(-90, 90), xy[1] + rng.uniform(-90, 90)]
               if name.startswith("AISLE ") else xy)
        for name, xy in noise["anchors"].items()}

    found = cal.fit(noise, guide)

    assert "x" not in found or not all(found["gates"].values())


def test_a_false_axis_candidate_does_not_hide_floor_derivation(monkeypatch):
    anchors = {f"AISLE {n}": [float(n), float(n)] for n in range(8)}
    geometry = {
        "page": {"w": 100, "h": 100},
        "anchors": anchors,
        "fixtures": [],
        "fixture_polys": [],
    }

    def candidates(_atlas, _guide, axis, source_axis, _extent):
        if source_axis != axis:
            return []
        return [{
            "scale": 2.0 if axis == 0 else 0.5,
            "offset": 0.0,
            "inliers": list(range(8)),
            "aisle_offset": 0,
            "source_axis": source_axis,
            "max_residual_pt": 1.0,
        }]

    monkeypatch.setattr(cal, "axis_candidates", candidates)
    found = cal.fit(
        geometry,
        geometry,
        floor=lambda pair: float(
            pair["x"]["scale"] == pair["y"]["scale"] == 0.5),
    )

    assert found["x"]["derived"]
    assert found["x"]["scale"] == found["y"]["scale"] == 0.5


def test_a_floor_fit_cannot_win_by_collapsing_the_store_footprint():
    geometry = {
        "fixtures": [[0, 0, 100, 100]],
        "fixture_polys": [],
    }
    collapsed = (
        {"scale": 0.2, "source_axis": 0},
        {"scale": 0.2, "source_axis": 1},
    )
    full_size = (
        {"scale": 0.8, "source_axis": 0},
        {"scale": 0.8, "source_axis": 1},
    )

    assert not cal._footprint_ok(collapsed, geometry, geometry)
    assert cal._footprint_ok(full_size, geometry, geometry)


def test_footprint_rejection_names_its_own_gate(monkeypatch):
    geometry = {
        "page": {"w": 100, "h": 100},
        "anchors": {f"AISLE {n}": [n * 10, n * 10] for n in range(1, 11)},
        "fixtures": [[0, 0, 100, 100]],
    }
    candidate = {
        "scale": 1, "offset": 0, "source_axis": 0,
        "inliers": [1, 2, 3], "max_residual_pt": 0,
    }
    monkeypatch.setattr(cal, "axis_candidates",
                        lambda *args: [candidate | {"source_axis": args[3]}])
    monkeypatch.setattr(cal, "_footprint_ok", lambda *args: False)

    found = cal.fit(geometry, geometry)

    assert found["gates"]["footprint"] is False
    assert found["gates"]["scale_skew"] is True
    assert "footprint" in found["notes"][-1]


def test_floor_gate_catches_a_transform_that_misses_the_floor(atlas, profile):
    good = cal.fit(atlas["geometry"],
                   json.load(open("data/659/geometry.json")),
                   cal.store_config("659")["atlas_fit"])
    assert cal.floor_check(profile, good, atlas["psas"])["pass"]

    drifted = copy.deepcopy(good)
    drifted["y"]["offset"] += 55        # a quarter of the store, southwards

    assert not cal.floor_check(profile, drifted, atlas["psas"])["pass"]


def test_a_psa_thrown_clean_off_the_map_is_recorded_as_json_can_hold_it(
        atlas, profile):
    """Infinity is what "off the map" measures, and not a thing JSON has:
    written literally it makes calibration.json a file no strict reader
    loads."""
    thrown = cal.fit(atlas["geometry"],
                     json.load(open("data/659/geometry.json")),
                     cal.store_config("659")["atlas_fit"])
    thrown["y"]["offset"] -= 10_000     # every PSA lands outside the drawing

    checked = cal.floor_check(profile, thrown, atlas["psas"])

    assert not checked["pass"]
    assert checked["off_floor"][0][1] is None      # no distance, worst first
    json.dumps(checked, allow_nan=False)           # what a strict reader does


def test_aisle_label_shift_is_per_store_data():
    config = cal.store_config("659")

    assert cal.guide_aisle_name(config, 17) == "AISLE 17"
    assert cal.guide_aisle_name(config, 23) == "AISLE 27"
    assert cal.guide_aisle_name({}, 23) == "AISLE 23"


def test_aisle_label_shifts_can_describe_multiple_guide_vintages():
    config = {"aisle_label_shifts": [
        {"from": 1, "to": 14, "add": 15},
        {"from": 31, "to": 45, "add": 5},
    ]}

    assert cal.guide_aisle_name(config, 3) == "AISLE 18"
    assert cal.guide_aisle_name(config, 20) == "AISLE 20"
    assert cal.guide_aisle_name(config, 35) == "AISLE 40"


def test_659_ships_a_passing_calibration():
    assert cal.load_calibration("659")["verdict"] == "pass"
    assert cal.blocked_reason("659") is None
    assert cal.is_catalog_enabled("659")
    assert 659 in cal.catalog_store_ids()


def test_catalog_excludes_failing_and_unknown_stores():
    assert not cal.is_catalog_enabled("388")
    assert not cal.is_catalog_enabled("9999")
    assert 388 not in cal.catalog_store_ids()


def test_a_store_without_an_atlas_says_why_it_cannot_place_products():
    assert "Atlas" in cal.blocked_reason("9999")
    assert cal.load_calibration("9999") is None


def test_once_labels_have_spoken_the_reason_is_theirs_not_the_ties():
    """388: 'verify against live labels to decide; live labels disagree' is
    a contradiction — the verification already happened and decided."""
    reason = cal.blocked_reason("388")

    assert "disagree" in reason
    assert "verify against live shelf labels" not in reason


def test_every_captured_store_agrees_with_its_own_floor(store_id="24"):
    """A calibration on disk must still place products on walkable floor.

    Cheap insurance against a store being calibrated once and then having its
    map edited out from under the transform.
    """
    for store in ("659", "24"):
        found = cal.load_calibration(store)
        if not found:
            continue
        atlas = cal.load_atlas(store)
        checked = cal.floor_check(cal.load_profile(store), found, atlas["psas"])
        assert checked["pass"], (store, checked["on_floor_pct"])


def test_live_labels_can_settle_a_tie_the_drawing_cannot():
    """The margin gate's own message sends you to --verify, so a passing live
    check has to clear it — otherwise the gate is unresolvable and the store
    is blocked forever (store 811, 2026-07-24)."""
    record = {"gates": {"margin": False, "floor": {"pass": True}}, "notes": []}
    calibrate_cli.resolve_live_gates(
        record, {"pass": True, "checked": 8, "agreed": 8, "misses": []})

    assert record["gates"]["margin"] is True
    assert "live labels" in record["notes"][-1]


def test_live_label_gate_tolerates_one_stale_catalog_item():
    assert calibrate_cli.labels_pass(11, 10)
    assert not calibrate_cli.labels_pass(11, 9)
    assert not calibrate_cli.labels_pass(5, 5)


def test_a_failed_live_check_leaves_the_tie_unresolved():
    record = {"gates": {"margin": False}, "notes": []}
    calibrate_cli.resolve_live_gates(
        record, {"pass": False, "checked": 8, "agreed": 6,
                 "misses": [{"product": "milk"}]})

    assert record["gates"]["margin"] is False
    assert record["notes"] == []


def test_live_labels_can_settle_a_small_nonretail_floor_tail():
    record = {"gates": {"margin": True, "floor": {
        "pass": False, "on_floor_pct": 97.2}},
        "notes": []}
    calibrate_cli.resolve_live_gates(
        record, {"pass": True, "checked": 11, "agreed": 11, "misses": []})

    assert record["gates"]["floor"]["pass"] is True


def test_verification_does_not_paper_over_a_bad_floor_or_other_gate():
    record = {"gates": {"margin": False, "floor": {
        "pass": False, "on_floor_pct": 96.9},
                        "residual": False}, "notes": []}
    calibrate_cli.resolve_live_gates(
        record, {"pass": True, "checked": 9, "agreed": 9, "misses": []})

    assert record["gates"]["floor"]["pass"] is False
    assert record["gates"]["residual"] is False


def test_a_product_deep_in_its_aisle_stays_in_its_aisle():
    """#811 prints aisle 4 at the front of a column and aisle 15 at the back
    of the same column. A product at the far end of aisle 4 is nearer the
    printed "15" than the printed "4" — the segment, not the point, decides."""
    anchors = {"AISLE 4": [590.0, 160.0], "AISLE 15": [594.0, 421.0],
               "ENTRANCE": [700.0, 700.0]}
    psas = {"01|4|A|1": [585.0, 180.0], "01|4|B|20": [595.0, 390.0]}
    runs = {("01", "4"): (0, 590.0)}
    point = [590.0, 380.0]

    segment = calibrate_cli.corridor_segment(psas, runs, "PSA:01:4", point)

    assert segment == [[590.0, 180.0], [590.0, 390.0]]
    assert calibrate_cli.nearest_aisle(anchors, segment) == "AISLE 4"
    # the old point-wise association is exactly the recorded 811 failure
    assert calibrate_cli.nearest_aisle(anchors, [point, point]) == "AISLE 15"


def test_badge_just_beyond_aisle_mouth_beats_closer_next_bank_badge():
    segment = [[695.0, 181.0], [695.0, 380.0]]
    anchors = {"AISLE 3": [698.0, 161.0], "AISLE 20": [695.0, 389.0]}

    assert calibrate_cli.aisle_agreement(
        anchors, "AISLE 3", segment) == ("AISLE 20", True)
    assert not calibrate_cli.label_fits_segment([668.0, 161.0], segment)
    assert not calibrate_cli.label_fits_segment([698.0, 100.0], segment)


def test_exact_nearest_badge_remains_enough_on_rotated_guides():
    anchors = {"AISLE 3": [10.0, 10.0], "AISLE 4": [90.0, 90.0]}

    assert calibrate_cli.aisle_agreement(
        anchors, "AISLE 3", [[10.0, 50.0], [10.0, 50.0]]
    ) == ("AISLE 3", True)


def test_a_department_blob_still_verifies_by_its_point():
    anchors = {"AISLE 2": [10.0, 10.0], "AISLE 9": [90.0, 90.0]}

    segment = calibrate_cli.corridor_segment({}, {}, "PSA:44|None", [12.0, 11.0])

    assert segment == [[12.0, 11.0], [12.0, 11.0]]
    assert calibrate_cli.nearest_aisle(anchors, segment) == "AISLE 2"
