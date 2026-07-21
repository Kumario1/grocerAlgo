# Universal Map Pipeline — Design

Date: 2026-07-21
Status: approved by user
Baseline: store 659's current walkable grid (`data/659/profile.npz` `free`), pixel by pixel.

## Problem

Store 659 reached "perfect" via universal code plus ~45 lines of hand-authored
per-store truth (zones, seal_zones, exclusions, inclusions). Onboarding the next
store must NOT require touching algorithm code. Goal: one frozen pipeline that
produces the best possible map for any store PDF, where all per-store variation
lives in small data files — authored, going forward, by a headless AI agent
(codex exec / claude -p) that views the QA maps and edits the JSONs.

## Decisions (user-confirmed)

1. **Data files, never code.** Universal rules in code; genuinely ambiguous
   spots resolved by per-store JSON. Store-specific code edits are forbidden.
2. **Store 24 to full quality in this pass**, truth authored by the executing
   agent from map reading, validating the exact loop the headless AI will run.
3. **Approach A** (auto-derived defaults + override files + golden gate) over
   self-tuning optimization (rejected: chicken-and-egg, overfit risk) and color
   segmentation (kept as future QA cross-check only — cannot reproduce the
   vector-built 659 baseline).

## Architecture

### 1. One universal pipeline

- `extract_659.py` → `extract.py` (already store-parameterized; rename).
  Vocabulary addition: BLOOMS recognized as floral-family label (H-E-B
  chain-wide branding; store 24 uses it).
- New `router/derive.py`:
  - `derive_zones(anchors)` → ENTRANCE from its extracted label, CHECKOUT from
    the CHECKSTANDS label. Missing → hard error with actionable message.
  - `derive_seal_zones(anchors, fixtures)` → service-dept anchors (STRICT name
    match against SERVICE_DEPTS + alias table, so noise anchors like
    "CANNED MEAT" cannot spawn zones) → disks `{pt, r: 130, bridge: 12}`;
    CHECKSTANDS → rect = padded bbox of the fixture cluster around the label,
    `bridge: 20`. All constants defined here once, universal.
  - A single shared loader (`load_store`) used by BOTH `build_profile.py` and
    `map_qa.py`, so profile and QA can never disagree on config.
- **Precedence: a per-store JSON, if present, wins verbatim** (full
  replacement, no merging). 659 keeps its files → bit-identical by
  construction. Store 24 runs on pure auto-derivation.

### 2. Golden gate — 659 pixel baseline

- `data/659/golden_free.npy`: today's 659 `free` grid, committed.
- `tests/test_golden.py`:
  - shipped `data/659/profile.npz` free grid `array_equal` golden (catches
    stale artifacts);
  - free grid rebuilt from committed inputs through the exact shared build
    path `array_equal` golden (catches rule drift), pixel by pixel.
- Re-bless procedure (documented in the test): regenerate golden + rerun 659
  walk tests + commit together with justification. Deliberate act, never drift.

### 3. Truth as data — tests included

- `data/<store>/walk_truth.json`: `{"must": [[x, y, "label"], ...],
  "must_not": [...]}` in PDF points.
- `tests/test_walkability.py` parametrizes over every store having
  `profile.npz` + `walk_truth.json`. 659's in-code point lists migrate
  verbatim. New store = new JSON, zero test edits.

### 4. Agent-loop interface (headless onboarding)

- Command surface: `./rebuild.sh <store>` — deterministic; nonzero exit on any
  hard failure.
- Machine-readable QA: `map_qa.py` writes `data/<store>/qa/report.json`:
  walkable/reachable %, components, culled pockets (size, centroid, nearest
  anchor), VERIFY flags (name, x, y, walkable fraction), far snaps, narrowest
  corridors, config provenance (each of zones/seal_zones: "file" | "derived"),
  and — when a store has override files — the informational delta between
  derived and file config (drift detector; 659 never silently diverges from
  the auto path).
- Agent MAY write: per-store `exclusions.json`, `inclusions.json`,
  `seal_zones.json`, `zones.json`, `walk_truth.json`.
  Agent MUST NOT touch: `router/`, `extract.py`, `tests/*.py`,
  `data/659/golden_free.npy`, other stores' data.
- Convergence (no new mechanism): VERIFY flags are resolved by an exclusion
  (seals as staff) or an inclusion (blesses as customer; inclusions already
  suppress VERIFY). **Done = `rebuild.sh <store>` exits 0 AND report.json has
  zero VERIFY flags.**
- `docs/onboarding.md`: the runbook written as a feedable prompt (inputs,
  loop, convergence, guardrails, truth-file schemas with 659 examples).

### 5. Store 24 bring-up (validation)

Execute the runbook as the headless agent would: rebuild on auto-defaults →
read report.json + QA PNGs → author 24's exclusions/inclusions (+ seal_zones
override only if auto rules fail badly; prefer exclusions) → walk_truth.json
(~12–18 points) → converge. Acceptance: `rebuild.sh 24` exits 0, zero VERIFY,
walk tests green for both stores, golden gate green.

### 6. Non-goals

- `app.py` stays single-store (659).
- No item-directory (resolve/CSV) work for 24.
- No color-segmentation QA layer (deferred).
- No interactive review UI (the headless agent is the review tool).

## Trade-offs accepted

- Per-store manual truth is bounded (~10–25 lines + walk points), not
  eliminated: the PDF has no "staff" attribute; some truth is irreducible.
- Auto-defaults are heuristics tuned on n=2; failures are visible (QA/tests)
  and fixed in data. A repeated failure pattern across stores justifies one
  universal rule change, gated by golden + all stores' walk tests.
- Pixel-exact golden freezes imperfections too; improvements pay the
  re-bless process cost. Accepted to make drift impossible.
- 659 never exercises the auto path (files win); mitigated by the
  drift-detector report.
