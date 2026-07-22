# grocerAlgo — in-store route optimizer

Paste a grocery list, get the provably shortest walking route through
H-E-B #659 (Austin). Powered by the store's official published directory
PDF — no scraping. See `plan.md` for the full product plan.

## Run it

    pip install -r requirements.txt
    python3 -m uvicorn app:app --port 8000
    # open http://localhost:8000

## Autonomous onboarding (any H-E-B store)

    ./pipeline.sh <store>             # store number in, audited map out
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
