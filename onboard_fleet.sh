#!/bin/sh
# Onboard one store inside a throwaway git worktree, so the fleet — ~380 of
# these — can run at once without the runs treading on each other's checkout.
#
#   ./onboard_fleet.sh <store> [city]
#   FLEET_REF=<commit-ish> ./onboard_fleet.sh <store>   branch it off elsewhere
#
# The worktree runs stages 1-5 with PIPE_NO_BROWSER=1. Stage 6 wants a
# logged-in .heb-<store> Chrome profile in the working directory, and a
# worktree has nowhere to keep one, so placement is deferred and done later as
# a batch from the main checkout.
#
# On success the store's data/<store>/ comes back here — plus the guide PDF
# discover downloaded, if this checkout does not already hold one — and the
# worktree is removed. On failure the worktree STAYS: the logs inside it are
# the only account of what went wrong, and deleting them to keep the tree tidy
# would throw away the whole point of having run it.
#
# No locking. Each instance owns one path, and git's worktree bookkeeping is
# already safe per-path; run one store per instance and there is no other
# shared state to guard.
set -e
S=$1
[ -n "$S" ] || { echo "usage: ./onboard_fleet.sh <store> [city]"; exit 2; }
shift
CITY=$1

ROOT=$(pwd)
WT="$ROOT/.wt/$S"
REF=${FLEET_REF:-HEAD}

echo "==> worktree for store $S at $WT ($REF)"
git worktree add "$WT" "$REF"

STATUS=0
if [ -n "$CITY" ]; then
    (cd "$WT" && PIPE_NO_BROWSER=1 ./pipeline.sh "$S" "$CITY") || STATUS=$?
else
    (cd "$WT" && PIPE_NO_BROWSER=1 ./pipeline.sh "$S") || STATUS=$?
fi

if [ "$STATUS" != 0 ]; then
    echo "==> store $S failed (exit $STATUS) — the worktree is LEFT for you:"
    echo "      $WT"
    # list what exists rather than the full set of names: a run that died in
    # discover has written none of them, and pointing at absent files reads
    # like the logs were lost rather than never made.
    echo "    the run's own account of it, newest first:"
    if ls "$WT/data/$S/qa"/*.log >/dev/null 2>&1; then
        ls -1t "$WT/data/$S/qa"/*.log | sed 's/^/      /'
    else
        echo "      (none — it did not get as far as writing one)"
    fi
    echo "    resume in place once you know where it broke:"
    echo "      (cd $WT && PIPE_NO_BROWSER=1 ./pipeline.sh $S --from <n>)"
    echo "    or discard it: git worktree remove --force $WT"
    exit "$STATUS"
fi

# Plain copy, no merge: the worktree is the only writer for this store, and
# trailing /. means this lands the same way whether or not data/<store> is here.
echo "==> copying store $S back into $ROOT"
mkdir -p "$ROOT/data/$S"
cp -R "$WT/data/$S/." "$ROOT/data/$S/"
echo "    data/$S/"

# discover.py treats any guide-*-<store>.pdf as "already have it", so this does
# too — a guide this checkout already holds is never clobbered by a fresh
# download that only differs in the city slug.
if ls "$ROOT"/guide-*-"$S".pdf >/dev/null 2>&1; then
    echo "    guide PDF already here, left alone"
else
    for pdf in "$WT"/guide-*-"$S".pdf; do
        [ -f "$pdf" ] || continue
        cp -R "$pdf" "$ROOT/"
        echo "    $(basename "$pdf")"
    done
fi

git worktree remove --force "$WT"
echo "==> store $S onboarded; its placement layer is still owed:"
echo "      python3 capture_atlas.py $S && python3 calibrate.py $S"
