#!/bin/sh
# Rebuild one store's map truth end-to-end: geometry -> profile -> QA -> tests.
# Usage: ./rebuild.sh [store]   (default 659)
set -e
S=${1:-659}
python3 extract.py "$S"
python3 build_profile.py "$S"
python3 map_qa.py "$S" | tee "data/$S/qa/stats.txt"
python3 -m pytest -q
