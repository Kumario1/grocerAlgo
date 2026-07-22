# Raster PDF Fallback Experiment and Production Plan

## Plan Artifacts

1. Save the previous store-onboarding plan verbatim to
   `docs/superpowers/plans/2026-07-22-store-265-raster-onboarding-plan.md`.
2. Save this standalone fallback plan to
   `docs/superpowers/plans/2026-07-22-raster-pdf-fallback-plan.md`.
3. Keep the plans separate: this plan must pass before executing the store 265
   onboarding plan.

## Goal and Fidelity Contract

Build a reusable fallback that converts an image-only store-map PDF into the same routing inputs produced by normal vector extraction:

- Complete sales-floor boundary, including entrances and exterior exclusions.
- Every shelf, counter, wall, fixture, checkout, and structural obstruction that affects routing.
- Exact contiguous aisle badge set and positions.
- Required department, entrance, exit, restroom, and checkout anchors.
- Positioned product-label OCR sufficient for the existing missed-section coverage checks.
- No routing-relevant detail may be silently omitted.

Full transcription of every tiny printed word is not an output requirement. Structural parity and QA coverage parity are mandatory.

The vector extractor remains untouched behaviorally. Raster processing activates only when the map page has no usable drawings/text and contains a full-page image.

## Phase 1: Build a Differential Benchmark

### Ground-truth corpus

Use the existing vector guides and committed geometry as authoritative references:

- Stores 24 and 659: development/tuning set.
- Stores 388 and 790: unseen validation set.
- Store 265: real scanned holdout, never used to tune universal thresholds.

For each vector guide, generate temporary image-only PDFs in three deterministic forms:

1. `clean`: 200 DPI RGB, JPEG quality 92.
2. `legacy`: rotated 90°, reduced contrast, 0.6 px blur, JPEG quality 70.
3. `hard`: 150 DPI, 0.75° skew, 1 px blur, JPEG quality 55.

Use fixed random seeds. Keep generated PDFs and masks outside tracked source and delete them when the experiment finishes.

### Throwaway prototype

Create a clearly named throwaway terminal prototype runnable with one command. It must:

- Run candidate extractors against the corpus.
- Display the current candidate, per-store metrics, aggregate score, runtime, and failure gates.
- Allow opening source, intermediate masks, geometry overlay, and vector-vs-raster diff images.
- Write all artifacts under a temporary directory.
- Keep candidate algorithms as pure functions that can be moved into production.
- Be deleted after the winning combination is absorbed.

For every case, render:

- original raster;
- normalized/deskewed image;
- red-label mask;
- dark and light structural masks;
- detected badge candidates;
- fixture/wall/boundary overlay;
- false-positive and false-negative structural diff;
- anchor-to-ground-truth displacement overlay.

## Phase 2: Evaluate Candidate Combinations

### Image preparation candidates

Evaluate these independently before combining them with OCR:

- Direct embedded-image extraction versus 200/300 DPI page rendering.
- Otsu thresholding.
- CLAHE plus adaptive thresholding.
- Color-aware dual thresholding: preserve faint gray shelf lines while separating red labels and dark perimeter walls.
- Automatic deskew and all four right-angle orientations.

### Geometry candidates

Test three bounded approaches:

1. Horizontal/vertical morphological line extraction plus closed contours.
2. Canny edges plus probabilistic Hough segments and contour reconstruction.
3. Dual-threshold hybrid: morphology for shelves, Hough for diagonal walls, and contours for fixtures/boundary.

Required processing:

- Remove recognized text regions before structural line detection.
- Merge duplicate/parallel scan lines within a two-point tolerance.
- Convert detected geometry into existing `fixtures`, `fixture_polys`, `obstacle_paths`, and `boundary`.
- Close entrance gaps only for boundary discovery; preserve the real openings in emitted geometry.
- Reject rather than approximate when a unique outer boundary cannot be established.

### OCR candidates

Run and score:

1. Apple Vision at 0°, 90°, 180°, and 270°.
2. Tesseract CLI using sparse-text and single-line modes at each orientation.
3. Region-based hybrid:
   - color-component crops for red department labels;
   - individual black badge crops for aisle digits;
   - rotated shelf-band crops for product labels.

Normalize both OCR backends into one word record containing text, confidence, bounding box, and orientation.

Aisle reconstruction may fill unreadable digits only when:

- candidates form a regular horizontal or vertical run;
- at least two OCR values uniquely establish direction and offset;
- the completed global set is exactly `1..N` with no duplicates.

Otherwise extraction fails.

### Selection gates

A candidate is ineligible if any clean or legacy validation case misses a hard gate:

- Boundary-mask IoU: ≥0.98 clean, ≥0.96 legacy.
- Raw walkable-grid IoU against vector extraction: ≥0.95 clean, ≥0.92 legacy.
- Tolerance-aware obstacle precision and recall: ≥0.93 clean, ≥0.88 legacy.
- Every baseline structural component longer than 12 PDF points overlaps the fallback result.
- Exact aisle-number set; median position error ≤4 points clean and ≤8 legacy.
- Required major-anchor recall: 100%; no false major anchors.
- Shelf/product-label recall near fixtures: ≥90% clean and ≥80% legacy, with coverage in every store wing.
- Runtime: ≤60 seconds with Vision and ≤120 seconds with Tesseract.

Among candidates passing all gates, rank by:

- 40% raw-grid/obstacle accuracy.
- 20% boundary accuracy.
- 20% anchor accuracy.
- 15% positioned-label recall.
- 5% runtime.

Any visible missing wall, shelf, entrance, or corridor is a veto regardless of score. If no candidate passes, continue only in the throwaway prototype; do not weaken gates or integrate a partial fallback.

## Phase 3: Store 265 Holdout QA

Run the winning, untuned universal combination against the correct store 265 scan.

### Mechanical requirements

- Detect aisles exactly `1..25`.
- Detect and position at minimum:
  - Entrance and Exit;
  - Checkstands;
  - Produce, Bakery, Deli, Seafood, Market, Dairy;
  - Tortilleria, Sushiya, Floral, Pharmacy;
  - Restrooms and Business Center.
- Find a unique outer boundary and both customer entrance openings.
- Produce enough fixtures and structural segments to represent every visible shelf/counter group.
- Build a raw grid where all 25 aisle corridors are reachable from their mouths.

### Visual requirements

Perform a 3×3 crop sweep at high zoom:

- Compare every crop against the source scan.
- Trace each aisle corridor end to end.
- Confirm every shelf edge, perimeter wall, service counter, checkout bank, restroom, pharmacy enclosure, and entrance gap.
- Mark false positives and false negatives directly on a diff overlay.
- Any missing routing-relevant structure fails the fallback.

Spot-check at least 30 printed product labels across all store wings:

- At least 90% must be recognized and positioned near the correct fixture.
- Every wing must achieve at least 80%.
- Errors must not remove the coverage net's ability to identify a swallowed section.

If store 265 needs calibration, allow only a reviewed `data/265/raster.json` containing proven necessities such as rotation or threshold. Do not add speculative per-store geometry or manually bypass extraction.

## Phase 4: Production Integration

Absorb only the winning prototype functions into one raster helper used by extraction and QA.

### Runtime behavior

- `extract.py` classifies page 2 as vector or raster.
- Vector pages continue through the existing path unchanged.
- Raster pages:
  1. extract/render and orient the map image;
  2. run Vision on macOS;
  3. retry with Tesseract when Vision is unavailable or fails invariants;
  4. extract geometry and OCR words;
  5. run all structural hard gates;
  6. write normal `geometry.json` and extraction overlay.

Add to raster geometry:

- `source_kind: "raster"`;
- applied rotation;
- normalized OCR word records.

Map QA and coverage checks must use the correctly rotated source image and stored OCR words.

Add OpenCV as the only image-processing dependency. Use Apple Vision conditionally on macOS and invoke the Tesseract CLI directly to avoid an unnecessary Python wrapper. Document `brew install tesseract` and the corresponding Linux package.

### Failure behavior

Fail with an actionable diagnostic containing:

- backend attempted;
- orientation and threshold selected;
- missing aisle numbers or anchors;
- boundary/segment counts;
- path to intermediate QA artifacts;
- OCR installation instructions when neither backend is available.

Never silently fall back to empty OCR, approximate boundary boxes, the wrong city PDF, or an older vector map.

## Phase 5: Permanent Regression and QA Gates

Add focused production tests for:

- raster/vector page classification;
- orientation and coordinate transforms;
- Vision-to-normalized-word conversion;
- Tesseract TSV parsing and fallback selection;
- badge candidate grouping and sequence reconstruction;
- rejection of ambiguous/missing aisle runs;
- boundary selection and entrance preservation;
- OCR-word use in coverage checks;
- store 265's exact aisle and required-anchor set.

Keep one differential rasterization test against a known vector guide in the normal suite. Keep the broader four-store/degradation benchmark as a separately runnable verification command.

Final acceptance before executing the store 265 onboarding plan:

- Both Vision and Tesseract paths pass the relevant corpus gates.
- Store 265 passes the full mechanical and 3×3 visual sweep.
- Existing vector stores regenerate without changed geometry/profile behavior.
- Store 659 remains pixel-identical to its golden grid.
- Full test suite passes.
- Throwaway prototype and temporary corpus are deleted.
- Winning combination, rejected alternatives, scores, and QA screenshots are recorded in `docs/superpowers/specs/2026-07-22-raster-fallback-benchmark-results.md`.

Only after this gate passes should the separate store 265 onboarding plan run exclusions/inclusions, walk truth, audit, and final human verdict.
