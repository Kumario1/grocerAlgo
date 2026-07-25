#!/bin/sh
# Run the whole fleet: every "store city" line of stores.txt (written by
# sweep_stores.py) through onboard_fleet.sh, N stores at a time.
#
#   ./fleet_all.sh [concurrency]        default 3
#
# Rerunnable: a store that already has data/<N>/walk_truth.json here is skipped
# (the one truth file every onboarded store carries — zones/exclusions are optional),
# so a killed run resumes by just running it again. Each store's output goes
# to logs/fleet/<N>.log; a failure leaves its worktree at .wt/<N> with the
# real logs inside (see onboard_fleet.sh).
[ -f stores.txt ] || { echo "no stores.txt — run: python3 sweep_stores.py"; exit 2; }
mkdir -p logs/fleet
xargs -L1 -P"${1:-3}" sh -c '
    [ -e "data/$1/walk_truth.json" ] && { echo "skip   $1 — already onboarded"; exit 0; }
    echo "start  $1 ($2)"
    if ./onboard_fleet.sh "$@" > "logs/fleet/$1.log" 2>&1; then
        echo "DONE   $1"
    else
        echo "FAILED $1 — logs/fleet/$1.log, worktree .wt/$1"
    fi
' fleet < stores.txt
echo "fleet pass complete — each store still owes its placement batch:"
echo "  python3 capture_atlas.py <N> && python3 calibrate.py <N>"
