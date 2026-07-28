"""Earn the Atlas -> guide transform, or refuse it.

H-E-B's live Atlas and the printed guide are two axis-aligned drawings of one
floor, so one scale and offset per axis is the whole relationship. What is NOT
given is which Atlas aisle is which guide aisle: at #659 the left-hand column
was renumbered by 4 between the two vintages, and a store's numbering can shift
anywhere. This module searches that correspondence and then tries to disprove
what it found.

Why the fit is per axis, and why each axis uses a different set of aisles: an
aisle label is printed somewhere ALONG its run, chosen by eye, so only the
coordinate ACROSS the run identifies the aisle. On #659 the top row of aisles
agrees to ~1 pt in x while its y differs by up to 140 pt between the drawings,
and the left column is the mirror of that. Fitting both axes from one set of
anchors is how the first #659 calibration smeared a shelf run across two aisles.

Why consensus rather than trimming outliers: under any given aisle offset most
pairs are wrong, and a least-squares fit through mostly-wrong pairs starts in
the wrong place and stays there. Every 2-anchor hypothesis is cheap at this
size (tens of anchors), so we take the one the most anchors agree with.

The gates below are the point of the module. A fit that merely looks plausible
is what put ice cream two aisles from where it belonged.
"""
import glob
import json
import math
import os

import numpy as np
from scipy import ndimage

from router import engine
from router.derive import GUIDES_DIR

TOL_PT = 3.0          # an inlier's residual, in page points (#659 truth: 2.2)
MIN_INLIERS = 8       # per axis
MIN_SPREAD = 0.25     # of the atlas page extent the fitted anchors must span
SCALE_BAND = (0.2, 5.0)   # two renderings of one floor; outside this it collapsed
MAX_SKEW = 0.05       # |x scale / y scale - 1|; both drawings are to scale
FOOTPRINT_BAND = (0.45, 1.75)  # mapped fixtures must still cover the same store
MIN_MARGIN = 2        # inliers the winning offset must beat the runner-up by
MIN_ON_FLOOR = 0.99   # PSAs that must land on reachable floor
LIVE_MIN_ON_FLOOR = 0.97  # small non-retail Atlas tail, only with live proof
MAX_SNAP_M = 5.0      # nothing on a shelf is further than this from a corridor


# What a failed gate means to someone reading the store picker.
GATE_MEANING = {
    "residual": "the two drawings' aisles do not line up — recapture the Atlas",
    # Resolvable, and the message has to say how. It names the action, not the
    # command: asking the live catalog which aisle a product's own shelf label
    # sits in settles a tie no amount of offline geometry can, and the store
    # menu offers it — this text is read in the picker, not in a terminal.
    "margin": "two aisle correspondences fit the drawing equally well — "
              "verify against live shelf labels to decide",
    "scale_skew": "the two drawings disagree on scale — check the guide",
    "footprint": "the fitted drawings disagree on store size — check the guide",
    "floor": "products land off the shopping floor — the map or the fit is wrong",
    "labels": "live shelf labels disagree with where products land",
}


def aisle_anchors(anchors):
    """{aisle number: point} — the only anchors trustworthy enough to fit."""
    out = {}
    for name, point in anchors.items():
        if name.startswith("AISLE "):
            try:
                out[int(name.split()[1])] = point
            except ValueError:
                continue
    return out


def _consensus(points):
    """Line most anchors agree with. points: [(source, target, aisle)]."""
    best = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            (s1, d1, _), (s2, d2, _) = points[i], points[j]
            if abs(s2 - s1) < 1e-6:
                continue
            scale = (d2 - d1) / (s2 - s1)
            offset = d1 - scale * s1
            agree = [p for p in points
                     if abs(scale * p[0] + offset - p[1]) <= TOL_PT]
            if len(agree) > len(best):
                best = agree
    if len(best) < MIN_INLIERS:
        return None
    # Least squares over the consensus, re-collected until the set stops
    # moving: the two-anchor hypothesis fixes WHICH anchors agree, not the
    # line through them, and refitting moves the line. Reporting the residual
    # of a line fitted AFTER the last selection is how #790 came out at
    # 3.001 pt against a 3 pt bar — an anchor failing the threshold it had
    # just been selected under.
    for _ in range(6):
        fit = np.polyfit([p[0] for p in best], [p[1] for p in best], 1)
        agree = [p for p in points
                 if abs(fit[0] * p[0] + fit[1] - p[1]) <= TOL_PT]
        if len(agree) < MIN_INLIERS:
            return None
        if {p[2] for p in agree} == {p[2] for p in best}:
            break
        best = agree
    best = agree
    residual = max(abs(fit[0] * p[0] + fit[1] - p[1]) for p in best)
    return {"scale": float(fit[0]), "offset": float(fit[1]),
            "inliers": sorted(p[2] for p in best),
            "max_residual_pt": round(float(residual), 3)}


def axis_candidates(atlas_aisles, guide_aisles, axis, source_axis,
                    atlas_extent):
    """Every aisle offset that survives the per-axis gates, best first."""
    out = []
    numbers = sorted(atlas_aisles)
    span = max(len(numbers), len(guide_aisles)) + 1
    for shift in range(-span, span + 1):
        points = [(atlas_aisles[n][source_axis], guide_aisles[n + shift][axis], n)
                  for n in numbers if n + shift in guide_aisles]
        if len(points) < MIN_INLIERS:
            continue
        found = _consensus(points)
        if not found:
            continue
        inside = [p for p in points if p[2] in set(found["inliers"])]
        spread = max(p[0] for p in inside) - min(p[0] for p in inside)
        # The anchors must span real distance, and the fit must not collapse.
        # A slope-0 line through the left-hand column's near-constant x has a
        # beautiful residual and carries no information at all; on #659 those
        # fakes outnumber the true fits and several of them have MORE inliers.
        if (spread < MIN_SPREAD * atlas_extent
                or not SCALE_BAND[0] <= abs(found["scale"]) <= SCALE_BAND[1]):
            continue
        out.append(found | {"aisle_offset": shift, "source_axis": source_axis})
    return sorted(out, key=lambda c: (-len(c["inliers"]), c["max_residual_pt"]))


def derive_axis(axis, known, atlas_extent, guide_extent, score,
                source_axis=None):
    """Recover an axis the aisle numbers cannot fit, from the floor itself.

    A store whose numbered aisles all run in one row gives its labels no
    spread along the other axis — there is literally nothing there to fit, and
    every candidate is a slope-0 line through label noise. Stores 265 and 388
    are both like this.

    Both drawings are to scale, which is what the skew gate asserts elsewhere,
    so that axis's scale is the one the other axis measured. Only the offset is
    unknown, and the store's own walkable floor pins it: slide the shelves
    along the axis until they stop landing where nobody can walk. A shift of a
    whole aisle pitch is the thing this cannot rule out on its own, which is
    what the live label check is for.
    """
    source_axis = axis if source_axis is None else source_axis
    best = None
    span = abs(known["scale"]) * atlas_extent
    for scale in (abs(known["scale"]), -abs(known["scale"])):
        # Slide the mapped span across the guide page one point at a time.
        for low in range(int(-span), int(guide_extent) + 1):
            offset = float(low) if scale > 0 else float(low + span)
            candidate = {"scale": scale, "offset": offset,
                         "inliers": [], "aisle_offset": None,
                         "source_axis": source_axis, "derived": True,
                         "max_residual_pt": None}
            landed = score(candidate)
            if best is None or landed > best[0]:
                best = (landed, candidate)
    return best[1] if best and best[0] > 0 else None


def _pinned(atlas_aisles, guide_aisles, pin, axis, source_axis=None):
    """An explicitly authored correspondence: [first, last] on each side."""
    source_axis = axis if source_axis is None else source_axis
    (a0, a1), (g0, g1) = pin["atlas"], pin["guide"]
    numbers = [n for n in range(a0, a1 + 1) if n in atlas_aisles]
    shift = g0 - a0
    points = [(atlas_aisles[n][source_axis], guide_aisles[n + shift][axis], n)
              for n in numbers if n + shift in guide_aisles]
    if len(points) < 2:
        raise ValueError(f"pinned {pin} matches fewer than two aisles")
    fit = np.polyfit([p[0] for p in points], [p[1] for p in points], 1)
    residual = max(abs(fit[0] * p[0] + fit[1] - p[1]) for p in points)
    return {"scale": float(fit[0]), "offset": float(fit[1]),
            "inliers": [p[2] for p in points], "aisle_offset": shift,
            "source_axis": source_axis, "pinned": True,
            "max_residual_pt": round(float(residual), 3)}


def floor_map(profile):
    """Metres from every cell to the nearest cell a shopper can stand on.

    Exact and computed once, which is what makes it usable both as the gate
    and as the ranking signal. engine.snap answers the same question at
    request time by walking Chebyshev rings and taking the first hit, so it
    can name a cell up to √2 further than the true nearest — fine for choosing
    a route cell, but it would have this gate measuring its own search pattern
    rather than the transform. (The app keeps snap: erring toward the printed
    label is the safe direction at request time.)
    """
    free, reach, _, m_per_cell = profile
    standable = free & (reach.reshape(free.shape) >= 0)
    return ndimage.distance_transform_edt(~standable) * m_per_cell


def landings(profile, calibration, psas, away=None, sample=None):
    """How far off the shopping floor each PSA lands, in metres."""
    free, _, cell, _ = profile
    height, width = free.shape
    if away is None:
        away = floor_map(profile)
    carry, runs = transform(calibration), shelf_runs(psas)
    keys = list(psas)
    if sample:
        keys = keys[::max(1, len(keys) // sample)]
    for key in keys:
        area, aisle, _, _ = key.split("|")
        x, y = carry(to_corridor(runs, f"PSA:{area}:{aisle}", psas[key]))
        cx, cy = int(x // cell), int(y // cell)
        inside = 0 <= cx < width and 0 <= cy < height
        yield key, (float(away[cy, cx]) if inside else float("inf"))


def floor_scorer(profile, psas, sample=400):
    """Fraction of sampled PSAs that land somewhere a shopper could stand.

    Cheap enough to rank every candidate correspondence with, which is the
    point. Counting agreeing aisle LABELS cannot tell a correct offset from
    one the aisle pitch absorbed into its intercept, because both fit the
    labels equally well. The store's own floor can: the wrong one puts whole
    shelf runs where nobody can walk.
    """
    away = floor_map(profile)
    free, _, cell, _ = profile
    height, width = free.shape
    runs = shelf_runs(psas)
    keys = list(psas)
    if sample:
        keys = keys[::max(1, len(keys) // sample)]
    points = np.asarray([
        to_corridor(
            runs,
            f"PSA:{key.split('|', 2)[0]}:{key.split('|', 2)[1]}",
            psas[key],
        )
        for key in keys
    ])

    def score(calibration):
        if not len(points):
            return 0
        x, y = calibration["x"], calibration["y"]
        cx = ((x["scale"] * points[:, x["source_axis"]] + x["offset"])
              // cell).astype(int)
        cy = ((y["scale"] * points[:, y["source_axis"]] + y["offset"])
              // cell).astype(int)
        inside = (0 <= cx) & (cx < width) & (0 <= cy) & (cy < height)
        landed = np.full(len(points), np.inf)
        landed[inside] = away[cy[inside], cx[inside]]
        return float(np.mean(landed <= MAX_SNAP_M))
    return score


def _fixture_span(geometry):
    points = []
    for x1, y1, x2, y2 in geometry.get("fixtures", []):
        points.extend(((x1, y1), (x2, y2)))
    for polygon in geometry.get("fixture_polys", []):
        points.extend(polygon)
    if len(points) < 2:
        return None
    points = np.asarray(points)
    return np.quantile(points, 0.98, axis=0) - np.quantile(
        points, 0.02, axis=0)


def _footprint_ok(pair, atlas_geometry, guide_geometry):
    """Reject the tempting floor score produced by shrinking a whole store."""
    atlas_span = _fixture_span(atlas_geometry)
    guide_span = _fixture_span(guide_geometry)
    if atlas_span is None or guide_span is None or np.any(guide_span <= 0):
        return True
    mapped = np.asarray([
        abs(axis["scale"]) * atlas_span[axis["source_axis"]]
        for axis in pair
    ])
    ratios = mapped / guide_span
    return bool(np.all(
        (FOOTPRINT_BAND[0] <= ratios) & (ratios <= FOOTPRINT_BAND[1])))


def fit(atlas_geometry, guide_geometry, pin=None, floor=None):
    """Atlas -> guide transform plus everything needed to disbelieve it."""
    atlas_aisles = aisle_anchors(atlas_geometry["anchors"])
    guide_aisles = aisle_anchors(guide_geometry["anchors"])
    notes = [f"atlas aisles {len(atlas_aisles)}, guide aisles {len(guide_aisles)}"]
    result = {"notes": notes, "gates": {}}
    if pin:
        # An authored correspondence outranks anything the search could find:
        # #659's fit is frozen into its goldens and must not drift.
        axes = {name: _pinned(atlas_aisles, guide_aisles, pin[name],
                              axis, pin[name].get("source_axis"))
                for axis, name in ((0, "x"), (1, "y"))}
        notes.append("pinned by data/<store>/store.json")
        result |= axes
        result["gates"]["residual"] = all(
            a["max_residual_pt"] <= TOL_PT for a in axes.values())
        result["gates"]["margin"] = True
        result["gates"]["scale_skew"] = _skew_ok(axes["x"], axes["y"])
        return result

    if len(atlas_aisles) < MIN_INLIERS or len(guide_aisles) < MIN_INLIERS:
        notes.append("too few numbered aisles on one of the drawings")
        result["gates"]["residual"] = result["gates"]["margin"] = False
        result["gates"]["scale_skew"] = False
        return result

    extent = (atlas_geometry["page"]["w"], atlas_geometry["page"]["h"])
    candidates = {}
    for axis, name in ((0, "x"), (1, "y")):
        # source_axis != axis is a drawing rotated against the guide's: fitting
        # it as if it were upright produces a confident, wrong answer.
        candidates[name] = [
            c for source_axis in (axis, 1 - axis)
            for c in axis_candidates(atlas_aisles, guide_aisles, axis,
                                     source_axis, extent[source_axis])]
        candidates[name].sort(key=lambda c: (-len(c["inliers"]),
                                             c["max_residual_pt"]))

    # Choose both axes together: the scale-skew check is what separates the
    # true offset from a neighbour that the aisle pitch absorbed into its
    # intercept, and it can only be applied to a PAIR.
    pairs = [(x, y) for x in candidates["x"] for y in candidates["y"]
             if _skew_ok(x, y) and x["source_axis"] != y["source_axis"]]

    # A few stores have a bogus candidate along an axis their numbered aisles
    # do not actually span. Zero candidates used to be the trigger for floor
    # derivation, so one plausible-looking false line hid the valid transform.
    # Derive the counterpart for the strongest candidates whether the other
    # axis found lines or not; the footprint and floor gates disprove collapses.
    guide_extent = (guide_geometry["page"]["w"], guide_geometry["page"]["h"])
    if floor:
        for base in candidates["x"][:5]:
            source_axis = 1 - base["source_axis"]
            found = derive_axis(
                1, base, extent[source_axis], guide_extent[1],
                lambda candidate, other=base: floor(
                    {"x": other, "y": candidate}),
                source_axis,
            )
            if found:
                pairs.append((base, found))
        for base in candidates["y"][:5]:
            source_axis = 1 - base["source_axis"]
            found = derive_axis(
                0, base, extent[source_axis], guide_extent[0],
                lambda candidate, other=base: floor(
                    {"x": candidate, "y": other}),
                source_axis,
            )
            if found:
                pairs.append((found, base))

    skew_pairs = pairs
    pairs = [pair for pair in skew_pairs
             if _footprint_ok(pair, atlas_geometry, guide_geometry)]
    if not pairs:
        footprint_failed = bool(skew_pairs)
        notes.append(
            "axis fits collapse or expand the store footprint"
            if footprint_failed else
            "no pair of axis fits agrees on scale within "
            f"{MAX_SKEW:.0%}" if candidates["x"] and candidates["y"]
            else "no axis fit survived the spread gates")
        result["gates"]["residual"] = result["gates"]["margin"] = False
        result["gates"]["scale_skew"] = footprint_failed
        if footprint_failed:
            result["gates"]["footprint"] = False
        return result
    # Rank by where the products actually land first, and only then by how
    # many anchors agree. Store 24's aisles are evenly enough spaced that the
    # correspondence two aisles off had MORE agreeing anchors than the true
    # one, and put a fifth of the store's shelves off the shopping floor.
    def rank(pair):
        # Rounded to whole percent: a fraction of a percent is sampling noise,
        # and below that the anchors are the better witness.
        landed = round(floor({"x": pair[0], "y": pair[1]}), 2) if floor else 0
        return (landed, len(pair[0]["inliers"]) + len(pair[1]["inliers"]),
                -max(_residuals(pair), default=0.0))

    pairs.sort(key=rank, reverse=True)
    x, y = pairs[0]
    if x.get("derived") or y.get("derived"):
        name = "x" if x.get("derived") else "y"
        notes.append(f"{name}: aisle labels did not constrain this axis, so "
                     "its scale came from the other axis and its offset from "
                     "the walkable floor")

    # An evenly spaced aisle grid fits almost as well one aisle over, so the
    # winner has to be clearly better than the best alternative correspondence
    # — either it lands more products on the floor, or more anchors agree.
    rival = next((p for p in pairs
                  if (p[0]["aisle_offset"], p[1]["aisle_offset"])
                  != (x["aisle_offset"], y["aisle_offset"])), None)
    def shifts(pair):
        return " ".join(
            f"{name}{axis['aisle_offset']:+d}" if axis["aisle_offset"] is not None
            else f"{name}=floor" for name, axis in zip("xy", pair))

    if rival:
        best_rank, rival_rank = rank((x, y)), rank(rival)
        margin = best_rank[1] - rival_rank[1]
        clear = (best_rank[0] - rival_rank[0] >= 0.01) or margin >= MIN_MARGIN
        notes.append(
            f"aisle offsets {shifts((x, y))}: {best_rank[0]:.0%} of sampled "
            f"shelves land on a walkable cell and {best_rank[1]} anchors agree, "
            f"against {rival_rank[0]:.0%}/{rival_rank[1]} for {shifts(rival)}")
    else:
        clear = True
        notes.append(f"aisle offsets {shifts((x, y))}, no rival correspondence")
    if x["source_axis"] != 0:
        notes.append("atlas appears rotated against the guide (axes swapped)")
    result |= {"x": x, "y": y}
    # A derived axis has no anchors to have a residual against; the floor and
    # label gates are what stand behind it.
    result["gates"]["residual"] = all(
        residual <= TOL_PT for residual in _residuals((x, y)))
    result["gates"]["margin"] = clear
    result["gates"]["scale_skew"] = True
    return result


def _residuals(pair):
    return [axis["max_residual_pt"] for axis in pair
            if axis.get("max_residual_pt") is not None]


def _skew_ok(x, y):
    if not x["scale"] or not y["scale"]:
        return False
    return abs(abs(x["scale"] / y["scale"]) - 1) <= MAX_SKEW


def transform(calibration):
    """Atlas point -> guide point."""
    x, y = calibration["x"], calibration["y"]

    def carry(point):
        return [float(x["scale"] * point[x["source_axis"]] + x["offset"]),
                float(y["scale"] * point[y["source_axis"]] + y["offset"])]
    return carry


def shelf_runs(psas):
    """Atlas centre-line of every (area, aisle) shelf run.

    A PSA is a spot on a shelf FACE, and an aisle's two faces straddle the
    corridor the shopper actually walks — so a product's pin belongs on the
    midline between them. Each run is long and thin, so the axis with the
    smaller extent is the one across it; anything squarer than 2:1 is a
    department blob rather than a run and is left alone.

    Taken from the Atlas's own PSA geometry, so it needs no correspondence
    between Atlas and guide aisle NUMBERS — which is just as well, because 40
    of #659's aisle numbers are reused by several departments at opposite ends
    of the store.
    """
    runs = {}
    for key, point in psas.items():
        area, aisle, _, _ = key.split("|")
        runs.setdefault((area, aisle), []).append(point)
    lines = {}
    for run, points in runs.items():
        if len(points) < 4:
            continue
        low = [min(p[a] for p in points) for a in (0, 1)]
        high = [max(p[a] for p in points) for a in (0, 1)]
        extent = [high[a] - low[a] for a in (0, 1)]
        axis = 0 if extent[0] < extent[1] else 1
        if extent[axis] * 2 > extent[1 - axis]:
            continue
        lines[run] = (axis, (low[axis] + high[axis]) / 2)
    return lines


def to_corridor(runs, group, point):
    """Move an Atlas shelf point onto the corridor of its own aisle."""
    line = runs.get(tuple(group.split(":")[1:]))
    if not line:
        return point
    axis, value = line
    point = list(point)
    point[axis] = value
    return point


def floor_check(profile, calibration, psas):
    """Every PSA measured against the floor, as the gate.

    The failure this exists to catch does not look like a failure: a wrong
    transform still puts points on a plausible-looking map, just inside the
    shelves instead of the corridor between them.
    """
    # A PSA the transform throws clean off the map measures inf, and inf is
    # not a thing JSON has: written literally it makes calibration.json a file
    # no strict reader will load. null says the same — there is no distance —
    # and parses, so it sorts first as the worst landing there is.
    worst = [(key, round(metres, 1) if math.isfinite(metres) else None)
             for key, metres in landings(profile, calibration, psas)
             if metres > MAX_SNAP_M]
    total = max(len(psas), 1)
    on_floor = total - len(worst)
    return {"psas": len(psas), "on_floor_pct": round(100 * on_floor / total, 2),
            "pass": on_floor / total >= MIN_ON_FLOOR,
            "off_floor": sorted(
                worst, key=lambda w: (w[1] is not None, -(w[1] or 0)))[:10]}


def load_profile(store):
    """(free, reach, cell, m_per_cell) for the store's accepted guide map."""
    data = np.load(f"data/{store}/profile.npz", allow_pickle=True)
    free = data["free"]
    names = [str(n) for n in data["names"]]
    entrance = tuple(int(v) for v in data["cells"][names.index("ENTRANCE")])
    reach, _ = engine.bfs(free, entrance)
    return free, reach, float(data["cell"]), float(data["m_per_cell"])


def store_config(store):
    """data/<store>/store.json — the only per-store overrides the app reads."""
    try:
        with open(f"data/{store}/store.json") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def atlas_dir(store):
    return store_config(store).get("atlas", f"data/{store}-atlas")


def load_atlas(store):
    """{'geometry', 'psas'} for a store, or None when none was captured."""
    directory = atlas_dir(store)
    try:
        with open(f"{directory}/geometry.json") as geometry, \
                open(f"{directory}/psas.json") as psas:
            return {"geometry": json.load(geometry), "psas": json.load(psas)}
    except FileNotFoundError:
        return None


def load_calibration(store):
    """A PASSING calibration, or None. Anything else must not place products."""
    try:
        with open(f"{atlas_dir(store)}/calibration.json") as handle:
            found = json.load(handle)
    except FileNotFoundError:
        return None
    return found if found.get("verdict") == "pass" else None


def blocked_reason(store):
    """Why this store cannot place products exactly, in one line, or None."""
    if load_atlas(store) is None:
        return "no Atlas captured yet — run capture_atlas.py"
    try:
        with open(f"{atlas_dir(store)}/calibration.json") as handle:
            found = json.load(handle)
    except FileNotFoundError:
        return "not calibrated yet — run calibrate.py"
    if found.get("verdict") == "pass":
        return None
    failed = [name for name, ok in found.get("gates", {}).items()
              if not (ok.get("pass") if isinstance(ok, dict) else ok)]
    if "labels" in failed:
        # The margin gate's advice is "verify against live labels" — once
        # the labels have spoken, the verdict is theirs, not the tie's.
        failed = [name for name in failed if name != "margin"]
    return "; ".join(GATE_MEANING.get(name, name) for name in failed) or \
        "calibration unverified"


def calibrate(store):
    """Fit and gate one store. Returns the calibration record."""
    atlas = load_atlas(store)
    if atlas is None:
        raise SystemExit(f"no Atlas for store {store} — run capture_atlas.py {store}")
    with open(f"data/{store}/geometry.json") as handle:
        guide = json.load(handle)
    config = store_config(store)
    profile = load_profile(store)
    record = fit(atlas["geometry"], guide, config.get("atlas_fit"),
                 floor=floor_scorer(profile, atlas["psas"]))
    record["store"] = str(store)
    if "x" in record:
        record["gates"]["floor"] = floor_check(profile, record, atlas["psas"])
    record["verdict"] = "pass" if all(
        (gate.get("pass") if isinstance(gate, dict) else gate)
        for gate in record["gates"].values()) else "fail"
    record.setdefault("verified", None)     # set by calibrate.py --verify
    return record


def write(store, record):
    path = os.path.join(atlas_dir(store), "calibration.json")
    with open(path, "w") as handle:
        json.dump(record, handle, indent=1)
        handle.write("\n")
    return path


def guide_aisle_name(config, number):
    """The guide anchor for a live H-E-B aisle number.

    #659's guide is an older vintage whose left-hand column was numbered four
    lower than today's shelf labels say. Any store can drift like this, so the
    correspondence is data (`aisle_label_shift`), and absent means identity.
    """
    shift = config.get("aisle_label_shift")
    if shift and number >= shift["from"]:
        number += shift["add"]
    return f"AISLE {number}"


def guide_city(store):
    """The city slug discover.py downloaded this store's guide under."""
    for path in sorted(glob.glob(f"{GUIDES_DIR}/guide-*-{store}.pdf")):
        name = os.path.basename(path)
        if name.endswith(f"-{store}.pdf"):
            return name[len("guide-"):-len(f"-{store}.pdf")]
    return None
