"""Per-store config loading + the one shared free-grid build path.

`load_store` and `build_free` are used by BOTH build_profile.py and
map_qa.py, so the profile and the QA renders can never disagree about a
store's walkable grid. tests/test_golden.py rebuilds 659 through this exact
path and compares it pixel-by-pixel against the frozen golden.

Precedence rule: a per-store JSON file, when present, wins VERBATIM (full
replacement, never merged with derived defaults).
"""
import glob
import json
import os

from router import engine


def pdf_path(store):
    """The store's guide PDF in the repo root: guide-<city>-<store>.pdf,
    any city (discover.py downloads it). Exact-suffix check so store 24
    never matches a store-124 file."""
    hits = sorted(p for p in glob.glob(f"guide-*-{store}.pdf")
                  if p.endswith(f"-{store}.pdf"))
    if not hits:
        raise SystemExit(f"no guide-*-{store}.pdf in the repo root — run: "
                         f"python3 discover.py {store}")
    if len(hits) > 1:
        raise SystemExit(f"ambiguous guide PDFs for store {store}: {hits}")
    return hits[0]

# --- auto-derivation constants (universal — never tuned per store; a store
# that needs different values authors its own seal_zones.json override) ---
#
# Departments that get an auto seal disk. FLORAL is deliberately NOT added
# to engine.SERVICE_DEPTS: that tuple drives SUBSTRING pocket-culling and
# the QA VERIFY pass, and 659's floral FRONT is real customer space (adding
# it fires a spurious VERIFY there — checked 2026-07-21, golden pixels
# unaffected but VERIFY != []). Strict-matched seal derivation is the only
# place the floral family needs.
SEAL_DEPTS = engine.SERVICE_DEPTS + ("FLORAL",)
DISK_R = 130        # pt — seal radius around a service-dept label (~15 m);
                    # 659's authored disks range 90-170
DISK_BRIDGE = 12    # pt — staff pass-through gap width (~1.4 m), same as
                    # the legacy global bridge
CHECK_WIN_X = 200   # pt — half-window around the CHECKSTANDS label in which
CHECK_WIN_Y = 45    # fixture centers count as checkstand-bank members
CHECK_PAD = 8       # pt — padding around the checkstand-cluster bbox
CHECK_BRIDGE = 20   # pt — checkout lanes are wider than counter gaps, so
                    # their zone bridges looser (matches 659's authored rect)


def _load_json(path):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return None


def derive_zones(anchors, store_dir="data/<store>"):
    """ENTRANCE/CHECKOUT zones straight from extracted labels.

    ENTRANCE <- the "ENTRANCE" anchor (else any anchor starting with
    ENTRANCE); CHECKOUT <- the "CHECKSTANDS" label position (else
    "CHECK STANDS", else any anchor starting with CHECK). A missing label
    is a hard, actionable error: the map cannot be onboarded without a
    human (or agent) authoring zones.json.
    """
    def find(exact, prefix):
        for name in exact:
            if name in anchors:
                return anchors[name]
        for name in sorted(anchors):
            if name.startswith(prefix):
                return anchors[name]
        return None

    ent = find(("ENTRANCE",), "ENTRANCE")
    chk = find(("CHECKSTANDS", "CHECK STANDS"), "CHECK")
    missing = [what for what, v in (("ENTRANCE", ent), ("CHECKSTANDS", chk))
               if v is None]
    if missing:
        raise SystemExit(
            f"cannot derive zones for {store_dir}: no {'/'.join(missing)} "
            f"label found among extracted anchors. Author "
            f"{store_dir}/zones.json with at least ENTRANCE and CHECKOUT "
            f"points (PDF pt), e.g. "
            '{"ENTRANCE": [x, y], "CHECKOUT": [x, y]}')
    return {"ENTRANCE": list(ent), "CHECKOUT": list(chk)}


def derive_seal_zones(anchors, fixtures):
    """Default seal zones from labels + fixtures (stores without a
    seal_zones.json override).

    Service disks: an anchor whose name STRICTLY equals a SEAL_DEPTS entry
    — or whose FIRST word aliases to one (engine.ALIASES, e.g. "BLOOMS
    RESTROOMS" -> FLORAL) — gets a disk {pt, r: DISK_R, bridge:
    DISK_BRIDGE}. Strictness is the point: noise anchors ("CANNED MEAT",
    "POT PIE FROZEN DESSERTS") must never spawn zones, and MEAT/DAIRY/
    PRODUCE are shelf departments, not service counters.

    Checkstand rect: fixtures (bboxes [x0,y0,x1,y1]) whose centers fall
    within +/-CHECK_WIN_X x +/-CHECK_WIN_Y of the CHECKSTANDS label form
    the checkstand bank; the zone is their bbox padded CHECK_PAD with the
    looser CHECK_BRIDGE.
    """
    zones = []
    for name in sorted(anchors):
        first = name.split()[0] if name.split() else ""
        family = engine.ALIASES.get(first)
        if name in SEAL_DEPTS or family in SEAL_DEPTS:
            zones.append({"name": f"auto:{name}", "pt": list(anchors[name]),
                          "r": DISK_R, "bridge": DISK_BRIDGE})
    label = anchors.get("CHECKSTANDS") or anchors.get("CHECK STANDS")
    if label is not None:
        lx, ly = label
        cluster = [f for f in fixtures
                   if abs((f[0] + f[2]) / 2 - lx) <= CHECK_WIN_X
                   and abs((f[1] + f[3]) / 2 - ly) <= CHECK_WIN_Y]
        if cluster:
            zones.append({"name": "auto:CHECKSTANDS",
                          "rect": [min(f[0] for f in cluster) - CHECK_PAD,
                                   min(f[1] for f in cluster) - CHECK_PAD,
                                   max(f[2] for f in cluster) + CHECK_PAD,
                                   max(f[3] for f in cluster) + CHECK_PAD],
                          "bridge": CHECK_BRIDGE})
    return zones


def _fixture_bboxes(geom):
    """Rect fixtures + poly-fixture bboxes, one [x0,y0,x1,y1] list."""
    boxes = [list(f) for f in geom["fixtures"]]
    for p in geom.get("fixture_polys") or []:
        xs, ys = [q[0] for q in p], [q[1] for q in p]
        boxes.append([min(xs), min(ys), max(xs), max(ys)])
    return boxes


def _zones_drift(file_zones, derived):
    """Compact informational diff between authored zones.json and what the
    deriver would produce (only ENTRANCE/CHECKOUT are derivable)."""
    fz = {k.upper(): v for k, v in file_zones.items()}
    diff = {}
    for k in ("ENTRANCE", "CHECKOUT"):
        a, b = fz.get(k), derived.get(k)
        if a != b:
            diff[k] = {"file": a, "derived": b}
    return diff or None


def _seal_drift(file_zones, derived):
    """Compact informational summary for an authored seal_zones.json vs the
    auto path (counts + names — geometry differences are expected; the
    file is hand-tuned, so this is a coverage comparison, not equality)."""
    return {"file_n": len(file_zones), "derived_n": len(derived),
            "file_names": [z.get("name", "?") for z in file_zones],
            "derived_names": [z.get("name", "?") for z in derived]}


def load_store(store_dir):
    """Read data/<store>/ into one config dict, deriving what is absent.

    Returns {geom, anchors, zones, exclusions, inclusions, seal_zones,
    provenance}. `anchors` is geometry anchors merged with zones keys
    uppercased — zone entries win, exactly as build_profile always did.

    Precedence: a per-store JSON, when present, wins VERBATIM and is
    marked "file" in provenance. Absent zones/seal_zones are auto-derived
    from the extracted labels ("derived"); absent exclusions/inclusions
    are simply empty ("absent" — there is no derivable default for human
    truth). When a store HAS override files, the derived config is still
    computed and the delta recorded in provenance["drift"] (informational
    drift detector — 659 never silently diverges from the auto path).
    """
    geom = json.load(open(os.path.join(store_dir, "geometry.json")))
    zones = _load_json(os.path.join(store_dir, "zones.json"))
    exclusions = _load_json(os.path.join(store_dir, "exclusions.json"))
    inclusions = _load_json(os.path.join(store_dir, "inclusions.json"))
    seal_zones = _load_json(os.path.join(store_dir, "seal_zones.json"))
    provenance = {name: ("file" if val is not None else "absent")
                  for name, val in (("zones", zones),
                                    ("seal_zones", seal_zones),
                                    ("exclusions", exclusions),
                                    ("inclusions", inclusions))}
    drift = {}

    if zones is None:
        zones = derive_zones(geom["anchors"], store_dir)   # may SystemExit
        provenance["zones"] = "derived"
    else:
        try:
            drift["zones"] = _zones_drift(zones,
                                          derive_zones(geom["anchors"]))
        except SystemExit as e:
            drift["zones"] = {"underivable": str(e)}
    anchors = {**geom["anchors"], **{k.upper(): v for k, v in zones.items()}}

    if seal_zones is None:
        seal_zones = derive_seal_zones(anchors, _fixture_bboxes(geom))
        provenance["seal_zones"] = "derived"
    else:
        drift["seal_zones"] = _seal_drift(
            seal_zones, derive_seal_zones(anchors, _fixture_bboxes(geom)))
    provenance["drift"] = {k: v for k, v in drift.items() if v} or None

    if "ENTRANCE" not in anchors or "CHECKOUT" not in anchors:
        raise SystemExit(
            f"{store_dir}/zones.json must define ENTRANCE and CHECKOUT "
            f"(has: {sorted(zones)})")
    return {"geom": geom, "anchors": anchors, "zones": zones,
            "exclusions": exclusions or [], "inclusions": inclusions or [],
            "seal_zones": seal_zones or [], "provenance": provenance}


def build_free(cfg, seed_name="ENTRANCE"):
    """The one free-grid build sequence (shared by profile + QA).

    build_grid(exclusions) -> seal_staff_gaps(seal_zones, aisle-badge
    protection, service-label condemnation) -> restore inclusion zones ->
    seed reachability from the entrance -> cut to the entrance-connected
    component.

    NOTE (frozen semantics — the 659 golden depends on them, see I4 in the
    plan): service_pts uses SUBSTRING matching against engine.SERVICE_DEPTS
    ("SEAFOOD SUSHI" or a merged noise line both condemn their pocket) and
    badge protection uses startswith("AISLE "). Strict name matching exists
    only in the seal-zone DERIVATION for stores without override files.

    Returns a dict:
        free        walkable grid cut to the entrance component (profile truth)
        free_uncut  pre-cut grid (QA renders isolated pockets from this)
        free_raw    post-exclusion grid before any sealing
        reach       flat BFS distance field from `seed` (over free_uncut)
        seed        (cx, cy) seed cell;  seed_pt: the PDF-point it came from
        culled      seal_staff_gaps culled-pocket list [(cells, x, y), ...]
        staff_mask  cells culled because a service label sat inside them
        incl_mask   bool mask of the inclusion shapes
    """
    geom, anchors = cfg["geom"], cfg["anchors"]
    free_raw = engine.build_grid(geom, exclusions=cfg["exclusions"])
    h, w = free_raw.shape

    if seed_name in anchors:
        seed_pt = anchors[seed_name]
    else:  # no entrance authored/derivable yet: seed from the aisle-badge
        # centroid, guaranteed in-store (QA-preview path)
        ax = [v for k, v in anchors.items() if k.startswith("AISLE")]
        seed_pt = (sum(x for x, _ in ax) / len(ax),
                   sum(y for _, y in ax) / len(ax))
        print(f"note: no {seed_name} anchor — "
              "seeding reachability from aisle centroid")

    badges = [v for k, v in anchors.items() if k.startswith("AISLE ")]
    service = [v for k, v in anchors.items()
               if any(s in k for s in engine.SERVICE_DEPTS)]
    free, culled, staff_mask = engine.seal_staff_gaps(
        free_raw, seed_pt, seal_zones=cfg["seal_zones"],
        protect_pts=badges, service_pts=service)
    incl_mask = engine.shape_mask(cfg["inclusions"], free.shape)
    free |= free_raw & incl_mask       # human-verified customer zones win

    seed = engine.nearest_free(free, seed_pt)
    reach, _ = engine.bfs(free, seed)
    free_uncut = free
    # the true walkable region is the entrance-connected component only —
    # enclosed rooms (lease, restrooms, back areas) get culled here
    free = free & (reach >= 0).reshape(h, w)
    return {"free": free, "free_uncut": free_uncut, "free_raw": free_raw,
            "reach": reach, "seed": seed, "seed_pt": seed_pt,
            "culled": culled, "staff_mask": staff_mask,
            "incl_mask": incl_mask}
