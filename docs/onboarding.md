# Store onboarding runbook (headless agent)

You are onboarding H-E-B store `<N>` into the grocerAlgo map pipeline.
This document is your complete instruction set. Follow it exactly.

## Mission

Input: `guide-austin-<N>.pdf` in the repo root (page 2 of the file is the
store map). Output: a converged walkable map for `data/<N>/` — meaning
`./rebuild.sh <N>` exits 0 AND `data/<N>/qa/report.json` has
`"verify": []` — plus an authored `data/<N>/walk_truth.json` locking the
result in as regression tests.

The pipeline is universal and frozen. ALL store-specific truth lives in
five JSON files under `data/<N>/`. You resolve every ambiguity by editing
data, never code.

## What the pipeline already does for you

`./rebuild.sh <N>` runs extract → build_profile → map_qa → pytest:

- `extract.py` reads the PDF: fixtures, walls, sales-floor boundary, aisle
  badges, department labels → `data/<N>/geometry.json` (+
  `extract_overlay.png` to eyeball what was captured).
- Zones (ENTRANCE/CHECKOUT) and seal_zones (service-counter disks +
  checkstand-lane rect) auto-derive from the extracted labels. A per-store
  JSON, if you author one, replaces the derived value VERBATIM (no
  merging).
- `map_qa.py` renders QA maps into `data/<N>/qa/` and writes
  `report.json` — your primary feedback signal.

## The loop

Repeat until converged:

1. `./rebuild.sh <N>` (a nonzero exit is itself a finding — read the
   error; e.g. "cannot derive zones" means the map lacks an
   ENTRANCE/CHECKSTANDS label and you must author `zones.json`).
2. Read `data/<N>/qa/report.json`. Look at, in order:
   - `verify`: service-dept labels whose surroundings are still walkable.
     EACH one demands a judgment call (step 3).
   - `components` / `culled_pockets`: one giant component is right;
     a large culled pocket near an aisle badge is a swallowed corridor.
   - `far_snaps`: an anchor snapping meters away from its label usually
     sits inside a wrongly-sealed or wrongly-open region.
   - `narrow`: corridors under ~0.5 m half-width are usually artifacts —
     slivers between a fixture and a wall that should be excluded.
   - `walkable_pct`: sane stores land roughly 20–35%.
3. View the PNGs in `data/<N>/qa/` (you can read images): green =
   walkable, purple = sealed staff areas, orange = walkable but cut off
   from the entrance, dashed red circles = the VERIFY spots.
   `corridor_width.png`: red = suspiciously tight. Compare against
   `data/659/qa/walkable_overlay.png` — that is what "converged" looks
   like.
4. For each VERIFY flag, decide from the map drawing:
   - Staff-only area (label sits inside a counter/prep enclosure) →
     add an entry to `data/<N>/exclusions.json`.
   - Legitimate customer space (label sits in an open corridor, e.g. a
     dept name printed on the shopping floor) → add an entry to
     `data/<N>/inclusions.json` (inclusions also suppress the VERIFY).
   PREFER exclusions/inclusions. Only author a full
   `data/<N>/seal_zones.json` override when the auto-derived zones are
   wrong in shape (e.g. the checkstand rect eats a corridor, or a service
   disk misses the counter entirely) — your file replaces ALL derived
   zones, so carry over the ones that were right.
5. Rerun. Converged = exit 0 AND `"verify": []` in report.json.

Then author `data/<N>/walk_truth.json` (~12–18 points, PDF pt, read off
the overlay PNG and `geometry.json` anchors):

- `must_not`: parking/outside-boundary points and page corners, the
  interior of EACH sealed staff area, enclosed rooms (lease, restrooms),
  a point between two checkstands (lane interior).
- `must`: the main front corridor, each department's customer frontage,
  the entrance approach, several distinct aisle corridors.

Points must describe ground truth about the STORE, not merely echo the
current grid — they are what stops future regressions. Rerun
`./rebuild.sh <N>` after authoring: all tests must pass.

## File schemas (verbatim 659 examples)

Every entry carries a `"name"` saying WHY — future readers get no other
context. Coordinates are PDF points on the map page.

`zones.json` — only when auto-derivation fails or needs correcting:

```json
{"ENTRANCE": [447.5, 612.3], "CHECKOUT": [600.0, 578.0]}
```

`exclusions.json` — drawing shows it open, shoppers can't use it
(rects or polys, blocked like fixtures):

```json
[
  {"name": "behind Dairy cases / top wall service band (cooler access, staff only)",
   "rect": [608, 58, 1030, 112]},
  {"name": "Pharmacy room behind the drawn counter wall (staff; wall traced from PDF strokes: x=321 wall, stair to (305,555), counter bay to (317,592))",
   "poly": [[262, 474], [321, 474], [321, 537], [305, 555], [288, 556],
            [288, 574], [317, 574], [317, 592], [262, 592]]}
]
```

`inclusions.json` — verified customer space the sealing rules over-culled
(restored after sealing; also suppresses VERIFY there):

```json
[
  {"name": "Pharmacy waiting corridor + aisle 41-45 west ends (customer)",
   "rect": [262, 455, 426, 592]}
]
```

`seal_zones.json` — full override of the derived seal zones (disk = service
counter, rect = checkstand bank; `bridge` = max gap width sealed, pt):

```json
[
  {"name": "Deli service island (staff behind counter; ~1.4 m pass-throughs)",
   "pt": [846, 277], "r": 170, "bridge": 12},
  {"name": "Checkstand lane bank — looser: checkout lanes are wider than service-counter gaps, so bridge more to seal the lanes while the wide front action-alley stays open",
   "rect": [405, 518, 762, 576], "bridge": 20}
]
```

`walk_truth.json`:

```json
{"must": [[652, 568, "front action alley south of checkstands"],
          [884, 500, "Floral front"]],
 "must_not": [[150, 150, "parking lot NW"],
              [851, 280, "Deli island interior (staff)"],
              [688, 545, "checkout lane between checkstands"]]}
```

## Guardrails (hard rules)

- You may create/edit ONLY these five files, ONLY for store `<N>`:
  `data/<N>/{zones,seal_zones,exclusions,inclusions,walk_truth}.json`.
- NEVER touch `router/`, `extract.py`, `build_profile.py`, `map_qa.py`,
  `tests/`, `rebuild.sh`, `data/659/golden_free.npy`, or any other
  store's `data/` directory.
- The full test suite (which includes store 659's pixel-frozen golden
  gate) must be green at the end — `./rebuild.sh <N>` runs it for you.
- If a problem seems to require a code change, or zones are underivable
  and the PDF genuinely lacks the labels: STOP and report the blocker
  with the evidence (report.json excerpt + what you saw on the PNG). Do
  not work around it in code.
