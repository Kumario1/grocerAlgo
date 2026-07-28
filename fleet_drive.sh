#!/bin/sh
# fleet_drive.sh — second-generation fleet driver. Sequential on purpose.
#
#   ./fleet_drive.sh                 drive every store in stores.txt
#   ./fleet_drive.sh 658 660         drive only these stores
#   FLEET_REF=<commit> ./fleet_drive.sh    pin worktrees to a commit
#   FLEET_RETRY=1 ./fleet_drive.sh   retry stores marked FLEET_FAILED
#
# What the first driver (fleet_all.sh) got wrong, and this one fixes:
#   - one store at a time: a usage-limit hit costs one stage of one store,
#     never a 200-store FAILED cascade, and the usage rate never spikes
#   - checkpoint-aware: each store resumes at its furthest completed stage
#     (worktree artifacts are the state — no state file to drift)
#   - promote-on-clean: the moment a store's pipeline exits green its data
#     is copied to the main checkout and COMMITTED, so finished work can
#     never be lost again
#   - placement-after-promotion: Atlas capture, calibration, and live tie-break
#     run in main, where each store's browser runtime persists across retries
#   - limit-parking: the session-limit message names its reset time; the
#     driver sleeps until then and retries the same store instead of
#     marking everything after it failed
#   - ref-healing: a worktree pinned to an older commit is rebuilt on the
#     current ref with its data/<store> preserved — code moves, truth stays
#
# ponytail: sequential loop, no locking, no state file. Parallelism can come
# back as N driver processes over disjoint lists if wall-clock ever matters
# more than usage.
set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
REF=${FLEET_REF:-$(git -C "$ROOT" rev-parse HEAD)}
LOG="$ROOT/logs/fleet"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3
mkdir -p "$LOG"

say() { echo "$(date '+%m-%d %H:%M') $*" | tee -a "$LOG/drive.log"; }

# One driver owns the shared main checkout and browser profile at a time.
# ponytail: PID file, not a daemon manager; replace if this ever runs on
# multiple hosts sharing one checkout.
PIDFILE="$LOG/driver.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    say "fleet_drive already running — pid $(cat "$PIDFILE")"
    exit 1
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 1' HUP INT TERM

# ---- state -----------------------------------------------------------------

# The furthest checkpoint decides what happens next. Echoes one of:
#   done | placement | calibration_blocked | blocked | failed | 1 | 3 | 4 | 5
# (5 = audit already CLEAN: rerun the mechanical verdict/output stage on the
# current ref, and its exit 0 is what triggers promotion — the pipeline stays
# the only judge of "green", the driver never re-implements it)
stage_for() {
    s=$1; wt="$ROOT/.wt/$s"; d="$wt/data/$s"
    if [ -f "$ROOT/data/$s/walk_truth.json" ]; then
        calibration="$ROOT/data/$s-atlas/calibration.json"
        if [ -f "$calibration" ]; then
            "$PYTHON" - "$calibration" <<'EOF'
import json, sys
record = json.load(open(sys.argv[1]))
verified = record.get("verified")
failed = [
    name for name, gate in record.get("gates", {}).items()
    if not (gate.get("pass") if isinstance(gate, dict) else gate)
]
retryable = verified is None and (
    record.get("verdict") == "pass" or failed == ["margin"])
print("done" if record.get("verdict") == "pass" and
      isinstance(verified, dict) and verified.get("pass")
      else "placement" if retryable or
      __import__("os").environ.get("FLEET_RETRY")
      else "calibration_blocked")
EOF
        else
            echo placement
        fi
        return
    fi
    [ -d "$wt" ] || { echo 1; return; }
    [ -z "${FLEET_RETRY:-}" ] && [ -f "$wt/FLEET_FAILED" ] && { echo failed; return; }
    if [ -f "$d/walk_truth.json" ]; then
        if [ ! -f "$d/qa/post_onboard.ok" ] &&
                { ! grep -Eq '[0-9]+ passed' "$d/qa/post_onboard.log" 2>/dev/null ||
                  grep -Eq '(^|[[:space:]])FAILED|[0-9]+ failed' "$d/qa/post_onboard.log" 2>/dev/null; }; then
            echo 3
            return
        fi
        if [ -f "$d/qa/audit.log" ]; then
            tail -5 "$d/qa/audit.log" | grep -q "AUDIT CLEAN" && { echo 5; return; }
            tail -5 "$d/qa/audit.log" | grep -q "AUDIT BLOCKED" && { echo blocked; return; }
        fi
        echo 4
        return
    fi
    [ -f "$d/qa/first_pass.log" ] && { echo 3; return; }
    echo 1
}

# Rebuild a worktree on $REF, carrying its data/<store> and guide PDF across.
# ponytail: cp to a temp dir, not git stash — the worktree is disposable, the
# data is not, and two plain copies are impossible to misread at 3am.
heal_ref() {
    s=$1; wt="$ROOT/.wt/$s"; keep="$LOG/.heal-$s"
    rm -rf "$keep"; mkdir -p "$keep"
    [ -d "$wt/data/$s" ] && cp -R "$wt/data/$s" "$keep/data"
    for pdf in "$wt"/guides/guide-*-"$s".pdf; do [ -f "$pdf" ] && cp "$pdf" "$keep/"; done
    git -C "$ROOT" worktree remove --force "$wt" 2>/dev/null \
        || { rm -rf "$wt"; git -C "$ROOT" worktree prune; }
    git -C "$ROOT" worktree add "$wt" "$REF" >/dev/null 2>&1 || return 1
    [ -d "$keep/data" ] && { mkdir -p "$wt/data"; cp -R "$keep/data" "$wt/data/$s"; }
    for pdf in "$keep"/guide-*.pdf; do
        [ -f "$pdf" ] || continue
        mkdir -p "$wt/guides"
        cp "$pdf" "$wt/guides/"
    done
    rm -rf "$keep"
}

# ---- outcomes --------------------------------------------------------------

promote() {
    s=$1; wt="$ROOT/.wt/$s"
    mkdir -p "$ROOT/data/$s"
    cp -R "$wt/data/$s/." "$ROOT/data/$s/"
    if ! ls "$ROOT"/guides/guide-*-"$s".pdf >/dev/null 2>&1; then
        mkdir -p "$ROOT/guides"
        for pdf in "$wt"/guides/guide-*-"$s".pdf; do
            [ -f "$pdf" ] && cp "$pdf" "$ROOT/guides/"
        done
    fi
    git -C "$ROOT" add -- "data/$s"
    paths="data/$s"
    for pdf in "$ROOT"/guides/guide-*-"$s".pdf; do
        [ -f "$pdf" ] || continue
        rel="guides/$(basename "$pdf")"
        git -C "$ROOT" add -- "$rel"
        paths="$paths $rel"
    done
    # shellcheck disable=SC2086 — $paths is intentionally word-split; guide
    # filenames never contain spaces (guides/guide-<city-slug>-<store>.pdf)
    git -C "$ROOT" commit -q -m "feat(data): store $s onboarded by fleet (audit clean)" \
        -- $paths 2>/dev/null \
        || say "store $s: nothing new to commit (already promoted?)"
    git -C "$ROOT" worktree remove --force "$wt" 2>/dev/null || true
    say "DONE   $s — map promoted to main"
}

place() {
    s=$1
    say "place  $s — Atlas capture + calibration in main"
    (cd "$ROOT" && PIPE_PYTHON="$PYTHON" ./pipeline.sh "$s" --from 6) \
        >> "$LOG/$s.log" 2>&1
    rc=$?
    if [ -d "$ROOT/data/$s-atlas" ]; then
        git -C "$ROOT" add -- "data/$s-atlas"
        git -C "$ROOT" commit -q -m "feat(data): calibrate store $s Atlas" \
            -- "data/$s-atlas" >/dev/null 2>&1 \
            || say "store $s: Atlas diagnostics already committed"
    fi
    if [ "$rc" = 0 ]; then
        say "READY  $s — map + placement passed"
    else
        say "BLOCK  $s — calibration gates failed; data/$s-atlas/calibration.json"
    fi
    return "$rc"
}

place_blocked_audit() {
    s=$1
    say "place  $s — audit blocked, checking placement independently"
    (cd "$ROOT/.wt/$s" &&
        HEB_RUNTIME_DIR="$ROOT/runtime/onboarding-$s" PIPE_PYTHON="$PYTHON" \
        ./pipeline.sh "$s" --from 6) >> "$LOG/$s.log" 2>&1
    say "BLOCK  $s — audit blocked, worktree kept: .wt/$s"
}

# Did a qa log WRITTEN DURING THIS RUN (newer than the mark file — stale
# limit lines from an earlier cascade sit at the tail of old logs) match a
# pattern? Prints matching tail lines.
recent_qa() {  # $1 store, $2 extended grep pattern
    for f in "$ROOT/.wt/$1/data/$1/qa/onboard.log" "$ROOT/.wt/$1/data/$1/qa/audit.log"; do
        [ -f "$f" ] && [ "$f" -nt "$LOG/.mark-$1" ] &&
            tail -3 "$f" | grep -Ei "$2" && return 0
    done
    return 1
}

park_until() {  # $1 = "3:30am"-style local time; sleeps until 2 min past it
    t=$(date -j -f '%I:%M%p' "$1" '+%s' 2>/dev/null || echo 0)
    now=$(date '+%s')
    [ "$t" -le "$now" ] && t=$((t + 86400))
    [ "$t" -le "$now" ] && t=$((now + 1800))          # unparseable → 30 min
    say "PARKED until $(date -r "$t" '+%H:%M') — usage limit"
    sleep $((t - now + 120))
}

# ---- the loop --------------------------------------------------------------

if [ $# -gt 0 ]; then
    LIST=$(for s in "$@"; do grep "^$s " "$ROOT/stores.txt" || echo "$s"; done)
else
    LIST=$(cat "$ROOT/stores.txt")
fi

say "fleet_drive start — ref $REF, $(echo "$LIST" | wc -l | tr -d ' ') stores"

while read -r S CITY; do
    [ -n "$S" ] || continue
    tries=0
    while :; do
        st=$(stage_for "$S")
        case $st in
            done)    say "skip   $S — already in main"; break ;;
            placement) place "$S"; break ;;
            calibration_blocked)
                say "BLOCK  $S — incompatible guide/Atlas; FLEET_RETRY=1 after source repair"
                break ;;
            failed)  say "skip   $S — FLEET_FAILED marker (FLEET_RETRY=1 to retry)"; break ;;
        esac

        WT="$ROOT/.wt/$S"
        if [ ! -d "$WT" ]; then
            git -C "$ROOT" worktree add "$WT" "$REF" >/dev/null 2>&1 \
                || { say "FAIL   $S — worktree add failed"; break; }
            for pdf in "$ROOT"/guides/guide-*-"$S".pdf; do
                [ -f "$pdf" ] || continue
                mkdir -p "$WT/guides"
                cp "$pdf" "$WT/guides/"
            done
        elif [ "$(git -C "$WT" rev-parse HEAD 2>/dev/null)" != "$(git -C "$ROOT" rev-parse "$REF")" ]; then
            say "heal   $S — worktree ref behind, rebuilding on $REF (data kept)"
            heal_ref "$S" || { say "FAIL   $S — worktree heal failed"; break; }
            continue      # recompute the stage off the healed tree
        fi
        if [ "$st" = blocked ]; then
            place_blocked_audit "$S"
            break
        fi

        say "start  $S${CITY:+ ($CITY)} — stage $st"
        touch "$LOG/.mark-$S"
        if [ "$st" = 1 ] && [ -n "$CITY" ]; then
            (cd "$WT" && PIPE_NO_BROWSER=1 ./pipeline.sh "$S" "$CITY") >> "$LOG/$S.log" 2>&1
        else
            (cd "$WT" && PIPE_NO_BROWSER=1 ./pipeline.sh "$S" --from "$st") >> "$LOG/$S.log" 2>&1
        fi
        rc=$?
        if [ "$rc" = 0 ]; then
            promote "$S"
            place "$S"
            break
        fi
        if tail -5 "$WT/data/$S/qa/audit.log" 2>/dev/null |
                grep -q "AUDIT BLOCKED"; then
            place_blocked_audit "$S"
            break
        fi

        if hit=$(recent_qa "$S" "session limit"); then
            reset=$(echo "$hit" | grep -o "resets [^ ]*" | awk '{print $2}' | tail -1)
            park_until "${reset:-unknown}"   # then retry this store, same spot
            continue
        fi
        tries=$((tries + 1))
        if [ "$tries" -lt 3 ] && recent_qa "$S" \
                "execution error|api error|connection (closed|error)|timed? out|temporarily unavailable|overloaded" \
                >/dev/null; then
            say "retry  $S — transient agent error ($tries/3), parking 60 sec"
            sleep 60
            continue
        fi
        touch "$WT/FLEET_FAILED"
        say "FAIL   $S (exit $rc) — logs/fleet/$S.log, worktree kept: .wt/$S"
        break
    done
done <<EOF
$LIST
EOF

say "fleet_drive pass complete — $(ls "$ROOT"/data/*/walk_truth.json 2>/dev/null | wc -l | tr -d ' ') stores in main"
rm -f "$PIDFILE"
