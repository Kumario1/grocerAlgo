# grocerAlgo — in-store route optimizer

Search H-E-B's live catalog, select exact products, and get the shortest
legal walking route through Lakeline H-E-B Plus! #659 (Austin). Product
placement uses H-E-B PALS data against the current 41-aisle Atlas map.
See `plan.md` for the full product plan.

## Run it

    pip install -r requirements.txt
    brew install tesseract             # macOS raster-PDF OCR fallback
    # apt install tesseract-ocr        # Debian/Ubuntu equivalent
    python3 -m uvicorn app:app --port 8000
    # open http://localhost:8000

On first run, choose **Connect H-E-B**. A persistent local Chrome profile
opens; select Lakeline H-E-B Plus! #659 there, return to the app, and confirm.
The profile is stored under `.heb-659/` and ignored by git. Credentials are
never stored by grocerAlgo.

The live Atlas profile was imported from the saved H-E-B product page:

    python3 import_heb.py "Fresh Sweet Cob Corn - Texas-Size Pack - Shop Corn at H-E-B.html"
    python3 build_profile.py 659-atlas

## Autonomous onboarding (any H-E-B store)

    ./pipeline.sh <store>             # store number in, audited map out
    ./pipeline.sh <store> Cedar Park  # city names are slugged automatically
    ./pipeline.sh <store> --no-agents # mechanical stages only (smoke test)

discover (probe + download the guide PDF from H-E-B's CDN) → rebuild →
onboarding agent (docs/onboarding.md) → adversarial audit agent
(docs/audit.md) → human visual verdict. The automated run reaches the final
gate only on `AUDIT CLEAN`, then prints `AWAITING VISUAL VERDICT`. The agent
runner defaults to isolated Opus/xhigh Claude sessions with subagents
disabled; override with `PIPE_AGENT`. Run it from a terminal.

## Universal map pipeline

    ./rebuild.sh <store>              # everything below + tests, one command

    guide-<city>-<store>.pdf          # discover.py finds + downloads this
      -> extract.py            geometry.json (fixtures, walls, boundary, labels)
      -> router/derive.py      per-store config: zones + seal_zones AUTO-DERIVE
                               from the extracted labels; a data/<store>/*.json
                               override, when present, wins verbatim
      -> build_profile.py      profile.npz (walkable grid, anchors, all-pairs D)
      -> map_qa.py             data/<store>/qa/ PNGs + report.json (machine-readable)

Vector guides keep the original extraction path. The image-only fallback is
currently an experiment: it passes the structural precision/recall, exact-aisle,
aisle-position, anchor and runtime gates on both OCR backends, but not the
boundary IoU gate, so normal pipeline runs reject raster guides. Benchmark and
holdout QA can opt in with `GROCER_RASTER_EXPERIMENTAL=1`. The experimental path
OCRs with Tesseract (it reads ~700 words per page to Apple Vision's ~400),
records positioned OCR in `geometry.json`, and writes diagnostics under
`data/<store>/qa/raster/`.

    python3 raster_benchmark.py --backend tesseract   # scores every plan gate,
    python3 raster_benchmark.py --backend vision      # exits nonzero on a miss

Per-case scores and the boundary root cause:
docs/superpowers/specs/2026-07-22-raster-fallback-benchmark-results.md

The algorithm is frozen and store-agnostic; ALL per-store variation lives
in small JSONs under data/<store>/ (zones, seal_zones, exclusions,
inclusions, walk_truth). Onboarding convergence = `./rebuild.sh <store>`
exits 0, report.json has zero VERIFY flags and empty coverage lists, and
the separate audit role reports AUDIT CLEAN. Final acceptance remains a
human visual verdict on the walkable and reachable graphs.

Golden gate: store 659's walkable grid is frozen pixel-by-pixel in
data/659/golden_free.npy (tests/test_golden.py). Any universal-rule
change that moves a blessed pixel fails the suite; re-blessing is a
deliberate documented act, never drift.

Per-store data lives in data/<store>/ (659 = pilot, 24 = first store
onboarded through the universal pipeline).

## Tests

    python3 -m pytest -q
