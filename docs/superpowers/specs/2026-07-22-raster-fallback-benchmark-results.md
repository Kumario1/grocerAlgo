# Raster Fallback Benchmark Results

Date: 2026-07-22

## Outcome

The store-265 holdout passes the implemented mechanical, OCR, raw-routing,
runtime, and visual gates with both Apple Vision and Tesseract. The broader
four-store differential does **not** yet satisfy every numeric acceptance gate
from the raster fallback plan, so the separate store-265 onboarding plan must
not be executed yet.

Production activation is therefore disabled by default. The raster path can
only be invoked for continued benchmark and holdout QA with
`GROCER_RASTER_EXPERIMENTAL=1`; normal pipeline runs stop with the benchmark
report path rather than activating the partial candidate.

The remaining blocker is differential parity with committed vector geometry,
not store 265: store 790 remains below clean obstacle-recall requirements and
a small number of long vector-reference components remain unmatched. The
current harness also does not yet compute boundary IoU, raw-grid IoU, or the
full anchor/label gates, so it cannot establish candidate eligibility. The
implementation therefore rejects failed runtime invariants and keeps the
benchmark independently runnable rather than hiding these results.

## Corpus and command

- Tuning: stores 24 and 659.
- Unseen validation: stores 388 and 790.
- Holdout: store 265 (never used for the vector differential thresholds).
- Deterministic cases: clean, legacy (90-degree rotation, lower contrast,
  0.6-pixel blur), and hard (150-DPI equivalent, 0.75-degree skew,
  one-pixel blur).
- Re-run: `python3 raster_benchmark.py --backend vision` and
  `python3 raster_benchmark.py --backend tesseract`.
- Temporary masks/diffs are written to `/tmp/grocerAlgo-raster-benchmark/`
  during a run and deleted after the recorded experiment.

The tolerance-aware precision/recall comparison uses the plan's two-PDF-point
tolerance (an 11-pixel kernel at 200 DPI). `long` is the share of committed
obstacle components longer than 12 PDF points that overlap the raster result.

## Final Apple Vision candidate

| Store | Case | Precision | Pixel recall | Long-component recall | Runtime |
|---|---:|---:|---:|---:|---:|
| 24 | clean | 0.976 | 0.941 | 0.994 | 10.2 s |
| 24 | legacy | 0.881 | 0.953 | 1.000 | 11.2 s |
| 24 | hard | 0.867 | 0.969 | 1.000 | 13.9 s |
| 659 | clean | 0.970 | 0.950 | 0.993 | 6.6 s |
| 659 | legacy | 0.893 | 0.965 | 0.994 | 11.0 s |
| 659 | hard | 0.888 | 0.982 | 0.997 | 12.8 s |
| 388 | clean | 0.972 | 0.938 | 1.000 | 4.6 s |
| 388 | legacy | 0.968 | 0.945 | 1.000 | 7.3 s |
| 388 | hard | 0.719 | 0.964 | 1.000 | 7.4 s |
| 790 | clean | 0.986 | 0.873 | 0.998 | 6.7 s |
| 790 | legacy | 0.882 | 0.899 | 0.998 | 11.1 s |
| 790 | hard | 0.861 | 0.938 | 1.000 | 12.1 s |

Vision is the leading candidate. It passes runtime, and store 388 passes the
reported obstacle gates, but store 790 clean recall is 0.873 and two of its
long components remain unmatched. Accurate Vision recognition is required;
fast mode produced only 86 legible positioned labels on store 265, while
accurate mode produced more than 900.

## Tesseract candidate

| Store | Case | Precision | Pixel recall | Long-component recall | Runtime |
|---|---:|---:|---:|---:|---:|
| 24 | clean | 0.982 | 0.933 | 0.994 | 7.8 s |
| 24 | legacy | 0.891 | 0.944 | 0.991 | 13.2 s |
| 24 | hard | 0.878 | 0.962 | 0.994 | 14.9 s |
| 659 | clean | 0.979 | 0.929 | 0.990 | 7.9 s |
| 659 | legacy | 0.897 | 0.941 | 0.993 | 13.2 s |
| 659 | hard | 0.891 | 0.972 | 0.999 | 14.1 s |
| 388 | clean | 0.975 | 0.920 | 1.000 | 5.7 s |
| 388 | legacy | 0.976 | 0.931 | 1.000 | 7.0 s |
| 388 | hard | 0.716 | 0.962 | 1.000 | 7.8 s |
| 790 | clean | 0.989 | 0.856 | 0.997 | 7.2 s |
| 790 | legacy | 0.882 | 0.896 | 0.998 | 11.7 s |
| 790 | hard | 0.862 | 0.936 | 0.997 | 13.3 s |

Tesseract passes store 265's exact mechanical and label-position test, but it
does not pass the full vector differential recall gates. The short-line
restoration experiment materially improved recall, but store 790 clean recall
is still 0.856. It remains available only behind the experimental guard: failed
aisle, anchor, label, boundary, structure, or raw-routing invariants abort with
installation and artifact diagnostics.

## Rejected alternatives

The throwaway prototype evaluated morphology-only, Hough-only, broad hybrid,
light morphology, and dual-threshold morphology before production work:

- Morphology-only: mean clean/legacy P 0.911, R 0.846, F1 0.877.
- Hough-only: P 0.780, R 0.757, F1 0.765.
- Broad hybrid: P 0.784, R 0.900, F1 0.836.
- Light morphology: P 0.864, R 0.929, F1 0.895.
- Dual threshold: P 0.882, R 0.906, F1 0.893.

Hough and broad hybrid admitted text as structure. Morphology-only missed
diagonal walls. The retained implementation uses color separation, positioned
OCR removal, long-line restoration, morphology for shelves, Hough only for
diagonal walls, contour fixtures, and compact-annotation filtering for boundary
discovery.

## Store 265 holdout

Both backends pass the permanent integration test:

- Aisles exactly 1 through 25, without duplicates.
- Entrance and exit pairs, Checkstands, Produce, Bakery, Deli, Seafood, Market,
  Dairy, Tortilleria, Sushiya, Floral, Pharmacy, Restrooms, and Business Center.
- Unique boundary, more than 75 fixture shapes, and more than 100 wall segments.
- Every aisle mouth is raw-grid reachable from the doorway-positioned entrance.
- A deterministic 35-label spot check spans all map wings; every label is
  recognized and positioned within 16 PDF points of a fixture or wall.
- Vision: approximately 13 seconds; Tesseract: approximately 8 seconds.

The 3x3 source-versus-overlay sweep found no missing routing-relevant shelf,
wall, service counter, checkout, pharmacy/restroom enclosure, corridor, or
entrance gap. The detailed normalized source, masks, badge candidates, and
geometry overlay are reproducibly generated in the ignored
`data/265/qa/raster/` directory; the reviewed extraction overlay is tracked:

![Store 265 extraction overlay](../../../data/265/extract_overlay.png)

## Regression status

- Vector extraction retains its original drawing/text behavior; regenerated
  geometry hashes for stores 24, 388, 659, and 790 are unchanged.
- Store 659's golden grid remains unchanged.
- Store 265's generated profile/report were deliberately removed after the
  downstream onboarding seal-zone stage failed; those artifacts belong to the
  separate store-onboarding plan.
- The throwaway prototype was deleted after its pure candidate functions moved
  into `router/raster.py`; `raster_benchmark.py` is the permanent verification
  command.

## Required next experiment

Do not weaken the plan thresholds. Improve universal boundary reconstruction
and add the missing boundary, raw-grid, aisle, anchor, and per-wing label
scoring to the harness. Generate the exact JPEG-compressed image-only PDF
corpus described by the plan rather than treating degraded images alone as the
corpus. Then improve store 790 obstacle recall, re-run both backend commands,
and require every gate before declaring the fallback accepted or starting
store 265 onboarding.
