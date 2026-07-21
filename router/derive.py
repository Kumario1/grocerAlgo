"""Per-store config loading + the one shared free-grid build path.

`load_store` and `build_free` are used by BOTH build_profile.py and
map_qa.py, so the profile and the QA renders can never disagree about a
store's walkable grid. tests/test_golden.py rebuilds 659 through this exact
path and compares it pixel-by-pixel against the frozen golden.

Precedence rule: a per-store JSON file, when present, wins VERBATIM (full
replacement, never merged with derived defaults).
"""
import json
import os

from router import engine


def _load_json(path):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return None


def load_store(store_dir):
    """Read data/<store>/ into one config dict.

    Returns {geom, anchors, zones, exclusions, inclusions, seal_zones,
    provenance}. `anchors` is geometry anchors merged with zones.json keys
    uppercased — zone entries win, exactly as build_profile always did.
    Missing optional files load as empty ([], {}) and are marked "absent"
    in provenance ("file" when present).
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
    zones = zones or {}
    anchors = {**geom["anchors"], **{k.upper(): v for k, v in zones.items()}}
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
