# Onboard H-E-B Store 265 from Its Scanned Guide

## Summary

Store 265's correct guide is the [`cedar-park-265` scan](https://images.heb.com/is/content/HEBGrocery/Store%20Finder%20Layouts/guide-cedar-park-265.pdf) for 170 E Whitestone, matching [H-E-B's official store #265 page](https://www.heb.com/heb-store/tx/cedar-park/hwy-183-and-whitestone-blvd-h-e-b-265). It has no PDF text or vector drawings, so the existing extractor cannot see aisles, fixtures, or walls.

The vector [`austin-265` guide](https://images.heb.com/is/content/HEBGrocery/Store%20Finder%20Layouts/guide-austin-265.pdf) is unsafe: its cover identifies 2800 E Whitestone, the other Cedar Park store. The solution is reusable raster extraction, not accepting that false match.

## Implementation Changes

- Preserve automatic city slugging, but validate the cover's address city—not its header—against the candidate slug. Download candidates to a temporary file and only retain one after validation; this rejects `austin-265` without leaving an ambiguous PDF.
- Extend `discover.py` validation to accept either:
  - existing vector guides with drawings, text, and aisle badges; or
  - two-page scanned guides with a sufficiently large full-page map image.
- Add a guarded raster branch in `extract.py`, backed by a shared `router/raster.py`:
  - render page 2 at source quality and rotate it into landscape coordinates;
  - use Apple Vision first on macOS, then the Tesseract CLI as fallback;
  - detect red department labels, black aisle badges, shelf/wall lines, closed fixtures, and the outer floor boundary;
  - reconstruct missing badge digits only from uniquely determined collinear sequences;
  - emit the existing geometry schema plus `rotation` and OCR `words`, leaving the vector path unchanged.
- Use OpenCV for line/contour detection. Add Mac Vision dependencies conditionally and document Tesseract installation for macOS/Linux. If neither backend exists, fail with an actionable install command.
- Add `data/265/raster.json` with reviewed calibration (`rotation: 270`, grayscale threshold around `220`); line-length values derive from render DPI rather than per-store constants.
- Rotate the PDF base image consistently in extraction and map QA overlays. Feed raster OCR words into the existing coverage checks so scanned guides retain shelf-frontage protection.

## Safety Gates and Interfaces

- Raster extraction must fail rather than guess unless it finds:
  - exactly one contiguous aisle run `1..N`, with store 265 resolving to `1..25`;
  - `ENTRANCE` and `CHECKSTANDS`, plus at least five recognized departments;
  - a boundary spanning over 60% of both page dimensions and enclosing over 25% of the page;
  - at least 50 structural segments and a nontrivial fixture set.
- OCR fallback is attempted when the primary backend errors or fails these invariants.
- `./pipeline.sh 265 Cedar Park` remains the public command.
- Existing vector stores and store 659's pixel-frozen golden must remain unchanged.

## Tests and Onboarding

- Add regression tests for multi-word city input, address-city mismatch, temporary-download cleanup, scanned-guide acceptance, OCR fallback selection, badge-sequence reconstruction, and raster coordinate rotation.
- Run raster extraction against the committed store 265 PDF and assert aisles `1..25`, required anchors, boundary/fixture minimums, and a visually aligned extraction overlay.
- Run the mechanical pipeline, then author store 265's exclusions, inclusions, seal zones if needed, and `walk_truth.json`.
- Acceptance requires:
  - full test suite green and store 659 golden unchanged;
  - one dominant reachable component and normal walkable percentage;
  - empty `verify` and coverage findings;
  - aisle/frontage truth points across every store wing;
  - independent `AUDIT CLEAN — store 265`;
  - final human visual verdict on the rendered overlays.

## Assumptions

- The official 2014 scanned guide is the accepted source of truth; no current in-store survey is required.
- Raster support should be reusable for future H-E-B scans.
- Apple Vision is preferred on this Mac, with Tesseract retained as the cross-platform fallback.
