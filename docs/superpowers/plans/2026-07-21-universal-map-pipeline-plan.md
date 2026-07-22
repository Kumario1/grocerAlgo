# Universal Map Pipeline — Execution Plan

Spec: `docs/superpowers/specs/2026-07-21-universal-map-pipeline-design.md`
(read it first). Work on branch `feat/universal-map-pipeline`. Commit per
task with clear messages. Do NOT push. Do NOT merge to main.

## Hard invariants (violating any = the task failed)

- **I1 — Golden pixels.** After every task, `data/659/profile.npz`'s `free`
  grid is `np.array_equal` to `data/659/golden_free.npy` (frozen in Task 0).
  Never regenerate the golden in this plan.
- **I2 — Data, not code, for store 24.** No universal constant or rule may be
  changed to fix a store-24 problem unless (a) it is justified as universal,
  (b) golden + full test suite stay green, and (c) the deviation is recorded
  in the final report. Prefer per-store JSON always.
- **I3 — Full suite green after every task.** `python3 -m pytest -q` (61 tests
  pass today; count only grows).
- **I4 — Preserve current pocket-culling semantics exactly.** In the current
  `build_profile.py`/`map_qa.py`, `service_pts` uses SUBSTRING matching
  (`any(s in k for s in engine.SERVICE_DEPTS)`) and badges use
  `k.startswith("AISLE ")`. The shared loader must reproduce this exactly or
  I1 breaks. The new STRICT matching applies ONLY to the new seal-zone
  derivation for stores without override files.

## Current-state facts (verified today; don't rediscover)

- `router/engine.py`: `CELL = 2.0` is the single resolution knob;
  `seal_staff_gaps(free, seed_pt, cell, seal_zones=(), max_pocket_cells=None,
  protect_pts=(), service_pts=())` does localized per-zone bridging; empty
  `seal_zones` → one global 12-pt bridge (legacy/toy-test path).
  `SERVICE_DEPTS = ("DELI", "BAKERY", "SEAFOOD", "SUSHI", "KITCHEN",
  "PHARMACY", "MEAL SIMPLE", "COOKING")`.
- Store 659 (`data/659/`): geometry.json, zones.json (ENTRANCE/CHECKOUT/
  CHECKSTANDS), seal_zones.json (4 dept disks + checkstand rect
  [405,518,762,576] bridge 20), exclusions.json (6 entries incl. cafe-table
  rect [982,412,1004,435]), inclusions.json, profile.npz (grid 633×417,
  walkable 24.0%, m_per_cell 0.2365), qa/. This is the blessed baseline state.
- Store 24 (`data/24/`): geometry.json + extract_overlay.png + stale qa/ only.
  Anchors include ENTRANCE [890,724], CHECKSTANDS [688,677], DELI, BAKERY,
  SEAFOOD, KITCHEN, PHARMACY, plus NOISE anchors ("CANNED MEAT",
  "POT PIE FROZEN DESSERTS", "BLOOMS RESTROOMS" — Blooms is H-E-B's floral
  brand merged with an adjacent Restrooms label). 43 aisle badges. Boundary
  11 verts. Its qa/stats.txt is STALE (old 4-pt run): VERIFY BAKERY 71%,
  VERIFY KITCHEN 43% — expect similar spots to need truth.
- `rebuild.sh` runs: `extract_659.py $S` → `build_profile.py $S` (if
  zones.json exists — this gate changes in Task 3) → `map_qa.py $S` → pytest.
- `tests/`: test_engine (toy grids, explicit `cell=4.0` args — fine, cell is
  a parameter), test_walkability (659-hardcoded MUST/MUST_NOT lists),
  test_api (659 app), test_directory, test_resolve. 61 pass.
- The working tree may contain uncommitted approved work (2-pt grid +
  localized seal zones + docs). Task 0 commits it first.

## Task 0 — Branch, commit current work, freeze golden

1. `git checkout -b feat/universal-map-pipeline`.
2. Commit the existing uncommitted work as-is (message:
   `feat: 2pt grid, localized seal zones, spec+plan docs`). Run
   `python3 -m pytest -q` first; must be green before committing.
3. Freeze the baseline BEFORE any further change:
   `python3 -c "import numpy as np; np.save('data/659/golden_free.npy', np.load('data/659/profile.npz', allow_pickle=True)['free'])"`.
4. New `tests/test_golden.py` (part 1 only): load golden, load
   `data/659/profile.npz`, assert shapes equal and `np.array_equal(free,
   golden)`. Docstring documents the re-bless procedure (regenerate golden +
   rerun 659 walk tests + commit together with justification; forbidden in
   this plan).
5. Verify: pytest green. Commit (`test: freeze 659 golden free grid`).

## Task 1 — Shared build path (`router/derive.py` skeleton)

Goal: one code path builds the free grid for build_profile, map_qa, and the
golden test. Bit-identical output (I1 proves it).

1. Create `router/derive.py` with:
   - `load_store(store_dir)` → dict: `geom`, `anchors` (geometry anchors
     merged with zones.json uppercased, exactly as build_profile does today),
     `exclusions`, `inclusions`, `seal_zones` (each `[]`/`{}` when file
     absent), plus `provenance` dict (per config: "file" or "absent" for now).
   - `build_free(cfg, seed_name="ENTRANCE")` → `(free, reach, seed, culled,
     staff_mask)`: build_grid(exclusions) → seal_staff_gaps(seal_zones,
     badges=`startswith("AISLE ")`, service_pts=SUBSTRING match — see I4) →
     `free |= free_raw & shape_mask(inclusions)` → nearest_free seed → bfs →
     `free &= reach>=0`. Exactly the current build_profile.py:23–50 sequence.
     NOTE map_qa's current flow differs slightly (it does NOT cut free to the
     entrance component before rendering orange "isolated pockets" — it
     renders `free & ~reachable`). Keep map_qa's rendering semantics by
     having build_free also return the pre-cut grid (`free_uncut`).
2. Rewire `build_profile.py` and `map_qa.py` through these functions. No
   behavior change; identical printed stats expected (spot-check by diffing
   `map_qa` stdout before/after).
3. Extend `tests/test_golden.py` (part 2): rebuild the free grid from
   committed inputs via `derive.load_store` + `derive.build_free`, assert
   `array_equal` with golden.
4. Verify: `./rebuild.sh 659` exit 0; pytest green (incl. both golden tests).
   Commit.

## Task 2 — Rename extractor + BLOOMS vocabulary

1. `git mv extract_659.py extract.py`; update `rebuild.sh`, README, any doc
   references. Keep the `[store]` argv interface.
2. Add `"BLOOMS"` to the extractor's known-label list (floral family). Add an
   alias table where the deriver can see it (Task 3): `ALIASES = {"BLOOMS":
   "FLORAL"}` — put it in `router/engine.py` next to SERVICE_DEPTS or in
   derive.py; single definition.
3. Rerun `python3 extract.py 659` — geometry.json must be byte-stable except
   possibly a new BLOOMS-derived anchor if that text exists on 659's map
   (it should not; 659 says "Floral").
4. Verify: `./rebuild.sh 659` exit 0, golden green, pytest green. Commit.

## Task 3 — Auto-derivation of zones + seal_zones

1. In `router/derive.py`:
   - `derive_zones(anchors)`: ENTRANCE ← anchor "ENTRANCE" (else any anchor
     starting "ENTRANCE"); CHECKOUT ← anchor "CHECKSTANDS" (else "CHECK
     STANDS", else any anchor starting "CHECK"). Return
     `{"ENTRANCE": [...], "CHECKOUT": [...]}`. Raise `SystemExit` with an
     actionable message naming the store dir and the file to author when a
     label is missing.
   - `derive_seal_zones(anchors, fixtures)`:
     - Service disks: for each anchor whose name STRICTLY matches a service
       dept — `name == dept`, or `ALIASES.get(word) == dept` for a name's
       first word (covers "BLOOMS ..." merged lines) — emit
       `{"name": f"auto:{name}", "pt": anchors[name], "r": 130, "bridge": 12}`.
       MEAT/DAIRY/PRODUCE are not service depts; noise anchors ("CANNED
       MEAT") must NOT match. Add FLORAL to SERVICE_DEPTS if not present —
       CAUTION: SERVICE_DEPTS is also used (substring) for pocket culling
       (I4); adding FLORAL changes 659's culling only if a pocket contains
       the FLORAL label — 659's floral area is open customer floor today, so
       verify golden stays green; if it flips a pixel, do NOT add FLORAL to
       SERVICE_DEPTS — instead give derive_seal_zones its own dept list
       `SERVICE_DEPTS + ("FLORAL",)`.
     - Checkstand rect: fixtures (rects + poly bboxes) whose centers fall
       within a window around the CHECKSTANDS anchor (start: ±200 pt x,
       ±45 pt y); rect = bbox of that cluster padded 8 pt, `bridge: 20`.
       Constants at module top with comments; tune ONLY against store 24's QA
       overlay (659 uses its file).
   - `load_store` now fills zones/seal_zones from these derivers when the
     file is absent; provenance records "derived". `build_profile.py` drops
     its `assert ENTRANCE/CHECKOUT` in favor of the deriver path;
     `rebuild.sh` drops the zones.json existence gate (build_profile now
     always runs; it hard-errors with the actionable message if underivable).
2. New `tests/test_derive.py` (toy anchors, no store data):
   - entrance/checkout derived from labels; SystemExit when missing;
   - DELI anchor yields a disk; "CANNED MEAT" and "POT PIE FROZEN DESSERTS"
     yield nothing; "BLOOMS RESTROOMS" yields a FLORAL-family disk;
   - checkstand cluster of synthetic fixtures yields a rect covering them,
     bridge 20;
   - file-precedence: load_store with a seal_zones.json present returns it
     verbatim, provenance "file".
3. Drift detector: when a store HAS override files, compute the derived
   config too and include a compact diff in provenance (for report.json in
   Task 5). Informational only.
4. Verify: `./rebuild.sh 659` (still all-"file" config) exit 0, golden green,
   pytest green. Commit.

## Task 4 — walk_truth.json + parametrized walkability tests

1. Create `data/659/walk_truth.json` from the MUST/MUST_NOT lists currently
   hardcoded in `tests/test_walkability.py` — verbatim points and names,
   schema `{"must": [[x, y, "name"], ...], "must_not": [...]}`.
2. Rewrite `tests/test_walkability.py`: discover stores via
   `glob("data/*/walk_truth.json")` requiring profile.npz beside it; for each
   store load profile (`CELL` from the npz — never hardcode) and parametrize:
   - every must/must_not point;
   - boundary containment (`FREE & ~boundary_mask` empty — port existing);
   - single connected component;
   - walkable fraction in (0.10, 0.40).
3. Verify: pytest — case count ≥ before for 659 (same points), all green.
   Commit.

## Task 5 — report.json + rebuild polish

1. `map_qa.py` writes `data/<store>/qa/report.json` alongside the prints:
   `{store, walkable_pct, reachable_pct, components: {n, sizes},
   culled_pockets: [{cells, x, y, near}], verify: [{name, x, y, frac}],
   far_snaps: [{name, cells, meters}], narrow: [{name, half_width_m}],
   provenance: {zones, seal_zones, exclusions, inclusions, drift}}`.
   Deterministic ordering (sort keys/lists). Same numbers as the prints.
2. `rebuild.sh`: already `set -e`; confirm nonzero exit propagates from every
   step (extract asserts, build_profile SystemExit, pytest).
3. Verify: `./rebuild.sh 659` → report.json exists, valid JSON,
   `verify == []`, provenance shows all "file". pytest green. Commit.

## Task 6 — `docs/onboarding.md` (the headless-agent runbook)

Write it as a feedable prompt. Contents:
- Mission: onboard store `<N>` from `guide-austin-<N>.pdf` to a converged map.
- Loop: `./rebuild.sh <N>` → read `data/<N>/qa/report.json` + view
  `walkable_overlay.png`, `reachable.png`, `corridor_width.png` → for each
  VERIFY flag decide staff (→ exclusions.json entry) vs customer
  (→ inclusions.json entry); for leaks/eaten corridors adjust per-store
  seal_zones.json (override) — prefer exclusions; → rerun until exit 0 and
  `verify == []`.
- Then author `walk_truth.json` (~12–18 points: parking/page corners, each
  sealed staff area, lease/restroom interiors, a checkout lane; MUST: main
  corridors, each dept frontage, entrance approach, several aisles).
- Schemas with real 659 examples (copy entries verbatim from its files;
  every entry carries a "name" explaining the why).
- Guardrails: may write only the five per-store JSONs for the target store;
  never touch `router/`, `extract.py`, `tests/`, goldens, other stores;
  convergence definition; escalate (stop and report) if underivable or if a
  fix seems to require code.
Commit.

## Task 7 — Store 24 bring-up (execute the runbook yourself)

1. `python3 extract.py 24` (fresh geometry with BLOOMS vocab), then
   `./rebuild.sh 24`. zones/seal_zones auto-derive (24 has ENTRANCE +
   CHECKSTANDS labels). Expect build_profile to succeed and QA to flag.
2. Sanity: `m_per_cell` within (0.1, 1.0) — the aisle-pitch heuristic is
   untested on 24's badge layout; if it degenerates (nan/absurd), that is a
   universal bug: fix in build_profile guarded by I1/I3, record deviation.
3. Iterate the Task-6 loop on store 24: view PNGs, resolve every VERIFY via
   exclusions/inclusions, check the auto checkstand rect visually sealed the
   lanes (compare against 659's look; loosen via per-store seal_zones.json
   override only if the auto rect is wrong).
4. Author `data/24/walk_truth.json` (~12–18 points as per runbook; derive
   coordinates from geometry.json anchors + overlay inspection, the way
   659's were chosen).
5. Done when: `./rebuild.sh 24` exit 0; report.json `verify == []`;
   `./rebuild.sh 659` still exit 0; full pytest green (659 + 24 + golden +
   derive); I1 intact.
6. Commit (`feat: store 24 onboarded via universal pipeline`).

## Task 8 — Final verification + report

1. Fresh full run: `./rebuild.sh 659 && ./rebuild.sh 24 && python3 -m pytest -q`.
2. README: one short section — pipeline diagram (extract → derive/override →
   profile → QA), onboarding pointer, golden-gate note.
3. Final report back (do not merge/push): per-task commits, test count
   before/after, 24's headline QA numbers (walkable %, VERIFY count = 0,
   narrowest corridors), all truth files authored for 24 with one-line
   rationale each, any I2 deviations with justification.
