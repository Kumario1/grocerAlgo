#!/bin/sh
# Rebuild one store's map truth end-to-end: geometry -> profile -> QA -> tests.
# Usage: ./rebuild.sh [store]   (default 659)
set -e
S=${1:-659}
python3 extract.py "$S"
python3 build_profile.py "$S"
# no pipe to tee: a pipeline's status is the LAST command's, which would
# swallow a map_qa failure under set -e
python3 map_qa.py "$S" > "data/$S/qa/stats.txt"
cat "data/$S/qa/stats.txt"
python3 -m pytest -q
