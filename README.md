# grocerAlgo — in-store route optimizer

Paste a grocery list, get the provably shortest walking route through
H-E-B #659 (Austin). Powered by the store's official published directory
PDF — no scraping. See `plan.md` for the full product plan.

## Run it

    pip install -r requirements.txt
    python3 -m uvicorn app:app --port 8000
    # open http://localhost:8000

## Universal map pipeline

    ./rebuild.sh <store>              # everything below + tests, one command

    guide-austin-<store>.pdf
      -> extract.py            geometry.json (fixtures, walls, boundary, labels)
      -> router/derive.py      per-store config: zones + seal_zones AUTO-DERIVE
                               from the extracted labels; a data/<store>/*.json
                               override, when present, wins verbatim
      -> build_profile.py      profile.npz (walkable grid, anchors, all-pairs D)
      -> map_qa.py             data/<store>/qa/ PNGs + report.json (machine-readable)

The algorithm is frozen and store-agnostic; ALL per-store variation lives
in small JSONs under data/<store>/ (zones, seal_zones, exclusions,
inclusions, walk_truth). To onboard a new store, follow
docs/onboarding.md — written to be executed by a headless agent; done =
`./rebuild.sh <store>` exits 0 and report.json has zero VERIFY flags.

Golden gate: store 659's walkable grid is frozen pixel-by-pixel in
data/659/golden_free.npy (tests/test_golden.py). Any universal-rule
change that moves a blessed pixel fails the suite; re-blessing is a
deliberate documented act, never drift.

Per-store data lives in data/<store>/ (659 = pilot, 24 = first store
onboarded through the universal pipeline).

## Tests

    python3 -m pytest -q
