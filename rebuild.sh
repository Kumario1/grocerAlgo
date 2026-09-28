#!/bin/sh
# Rebuild one store's map truth end-to-end: geometry -> profile -> QA -> tests.
# Usage: ./rebuild.sh [store]   (default 659, and runs the whole suite)
set -e
S=${1:-659}
python3 extract.py "$S"
python3 build_profile.py "$S"
# no pipe to tee: a pipeline's status is the LAST command's, which would
# swallow a map_qa failure under set -e
python3 map_qa.py "$S" > "data/$S/qa/stats.txt"
cat "data/$S/qa/stats.txt"

# This is an onboarding agent's inner loop, run ten-odd times per store, so it
# tests what a data edit can actually break: this store's own walk-truth and
# coverage, plus the goldens that any store's data can move. The whole suite
# grows with every store onboarded (520 tests before 811, 568 after) and is
# 71% of this script's runtime — pipeline.sh runs it once at the ship gate,
# which is where a cross-store regression has to be caught.
if [ -n "$1" ]; then
    # two runs, not one: -k filters the whole session, so folding the goldens
    # into the store-filtered run would deselect every one of them
    python3 -m pytest -q tests/test_walkability.py tests/test_coverage.py -k "$S"
    python3 -m pytest -q tests/test_golden.py tests/test_route_golden.py \
        tests/test_legality.py
else
    python3 -m pytest -q
fi
