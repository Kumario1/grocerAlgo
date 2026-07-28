#!/bin/sh
# Autonomous store-onboarding pipeline: unknown H-E-B store number in,
# converged + agent-audited map out, awaiting a human visual verdict.
#
#   ./pipeline.sh <store> [city]             full run (agents included)
#   ./pipeline.sh <store> --no-agents        mechanical stages only
#   ./pipeline.sh <store> [city] --from 6    resume at a stage
#   PIPE_NO_BROWSER=1 ./pipeline.sh <store>  stages 1-5, placement deferred
#
# Stages:
#   1. discover  — find + download + validate the store's guide PDF
#   2. rebuild   — extract -> profile -> QA (first pass may fail: that
#                  failure text is the onboarding agent's first input)
#   3. onboard   — headless agent runs docs/onboarding.md (edits only
#                  data/<store>/*.json truth files, loops to convergence)
#   4. audit     — a SEPARATE headless agent runs docs/audit.md
#                  adversarially; ships only on AUDIT CLEAN
#   5. output    — full test suite, data/<store>/profile.npz + qa/report.json
#                  + PNGs, marked AWAITING VISUAL VERDICT
#   6. placement — capture the store's live Atlas and calibrate it onto the
#                  guide, so products pin to the shelf they sit on instead of
#                  to the aisle label. Needs a browser session, so it is the
#                  one stage that may want a human present.
#
# Every stage is idempotent and reads its inputs off disk, so --from <n> skips
# straight to one. That matters because stages 3 and 4 are ~50 minutes of agent
# time: a failure at stage 5 must never cost them again.
#
# A blocked audit does not abandon stage 6. The store then lands in the picker
# with a named reason instead of vanishing, which is the honest end state and
# the one the app already knows how to render.
#
# PIPE_NO_BROWSER=1 skips stage 6 outright. The fleet uses it inside disposable
# map worktrees, promotes the clean map, then calls this pipeline at --from 6 in
# the main checkout. Nothing is attempted in the worktree, so its exit status
# remains the audit verdict alone.
#
# Agent runner: isolated Opus/xhigh Claude session (override the whole command
# with PIPE_AGENT='codex exec' etc.). Runs with --dangerously-skip-permissions
# — the guardrails live in the runbook prompts (data-files-only, no code, no
# goldens) and the golden + test gates catch violations. Subagent spawning is
# hard-disabled (--disallowedTools): one session per stage does all the work
# itself — slower, but every token spent is visible in one log and the usage
# rate stays flat. Run from a terminal, not from inside another agent session.
set -e
S=$1
[ -n "$S" ] || { echo "usage: ./pipeline.sh <store> [city|--no-agents] [--from n]"; exit 2; }
shift
PYTHON=${PIPE_PYTHON:-python3}
PATH=$(dirname "$PYTHON"):$PATH
export PATH
HEB_RUNTIME_DIR=${HEB_RUNTIME_DIR:-runtime/onboarding-$S}
export HEB_RUNTIME_DIR
if [ -z "${CHROME_PATH:-}" ] &&
        [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    export CHROME_PATH
fi

FROM=1
CITY=""
while [ $# -gt 0 ]; do
    case $1 in
        --from) FROM=$2; shift 2 ;;
        *) CITY="${CITY:+$CITY }$1"; shift ;;
    esac
done
case $FROM in
    [1-6]) ;;
    *) echo "--from takes a stage number 1-6"; exit 2 ;;
esac

# effort high, not medium: the 790 parity test showed medium ships maps that
# pass every automated gate yet seal real customer space (service-disk rims
# pinching aisle mouths) — and a medium audit blesses them. High reproduced
# the shipped xhigh map exactly (60/60 walk-truth points).
AGENT=${PIPE_AGENT:-"claude --safe-mode --model claude-opus-5 --effort high --disallowedTools Task,Agent --dangerously-skip-permissions -p"}
LOG="data/$S/qa"
mkdir -p "$LOG"
[ "$FROM" = 1 ] || echo "==> resuming store $S at stage $FROM"

if [ "$FROM" -le 1 ]; then
    echo "==> [1/6] discover: store $S"
    if [ "$CITY" = "--no-agents" ] || [ -z "$CITY" ]; then
        "$PYTHON" discover.py "$S"
    else
        "$PYTHON" discover.py "$S" "$CITY"
    fi
fi

if [ "$FROM" -le 2 ]; then
    echo "==> [2/6] first mechanical pass"
    if ./rebuild.sh "$S" > "$LOG/first_pass.log" 2>&1; then
        echo "    first pass clean (see $LOG/first_pass.log)"
    else
        echo "    first pass exit $? — expected before truth exists; the"
        echo "    onboarding agent starts from $LOG/first_pass.log"
    fi
fi

if [ "$CITY" = "--no-agents" ]; then
    echo "==> --no-agents: stopping after mechanical stages"
    exit 0
fi

if [ "$FROM" -le 3 ]; then
    echo "==> [3/6] onboarding agent (docs/onboarding.md, store $S)"
    { echo "You are in the grocerAlgo repo. Execute this runbook for store $S:"; \
      echo; sed "s/<N>/$S/g" docs/onboarding.md; \
      echo; sed "s/<N>/$S/g" docs/openings.md; \
      echo; sed "s/<N>/$S/g" docs/decorations.md; } | $AGENT > "$LOG/onboard.log" 2>&1 \
        || { echo "onboarding agent failed — read $LOG/onboard.log"; exit 1; }
    echo "    onboarding agent done ($LOG/onboard.log)"

    ./rebuild.sh "$S" > "$LOG/post_onboard.log" 2>&1 \
        || { echo "rebuild after onboarding failed — read $LOG/post_onboard.log"; exit 1; }
    touch "$LOG/post_onboard.ok"
fi

if [ "$FROM" -le 4 ]; then
    echo "==> [4/6] audit agent (docs/audit.md, store $S — fresh context)"
    { echo "You are the adversarial auditor in the grocerAlgo repo. Execute this runbook for store $S:"; \
      echo; sed "s/<N>/$S/g" docs/audit.md; \
      echo; sed "s/<N>/$S/g" docs/openings.md; \
      echo; sed "s/<N>/$S/g" docs/decorations.md; } | $AGENT > "$LOG/audit.log" 2>&1 \
        || { echo "audit agent failed — read $LOG/audit.log"; exit 1; }

    ./rebuild.sh "$S" > "$LOG/post_audit.log" 2>&1 \
        || { echo "rebuild after audit failed — read $LOG/post_audit.log"; exit 1; }
fi

# CLEAN describes the artifacts, not the sweep: an audit that finds three real
# defects, fixes them in data and re-verifies is a GOOD audit and ships. Store
# 811 read as a failure for a whole day because the old contract had no word
# for that outcome.
VERDICT=clean
PLACEMENT=ok
if [ "$FROM" -le 5 ]; then
    echo "==> [5/6] verdict + output"
    if grep -q "AUDIT CLEAN" "$LOG/audit.log" 2>/dev/null; then
        grep -m1 "AUDIT CLEAN" "$LOG/audit.log" | sed 's/^/    /'
    else
        VERDICT=blocked
        echo "    audit did NOT report clean — findings in $LOG/audit.log:"
        # AUDIT FAILED is the pre-2026-07-24 word, still in older logs
        grep -A2 "AUDIT BLOCKED\|AUDIT FAILED" "$LOG/audit.log" 2>/dev/null \
            | head -8 || tail -8 "$LOG/audit.log"
    fi
    # The store-filtered suite runs on every rebuild; this is the one place a
    # regression in ANOTHER store has to be caught before this one ships.
    echo "    full suite (every store, goldens included)"
    # not piped: a pipeline's status is the LAST command's, which would swallow
    # a failing suite under set -e
    "$PYTHON" -m pytest -q > "$LOG/full_suite.log" 2>&1 \
        || { echo "    SUITE FAILED — read $LOG/full_suite.log"; exit 1; }
    tail -1 "$LOG/full_suite.log" | sed 's/^/    /'
    "$PYTHON" - "$S" <<'EOF'
import json, sys
r = json.load(open(f"data/{sys.argv[1]}/qa/report.json"))
print(f"    walkable {r['walkable_pct']}%  reachable {r['reachable_pct']}%  "
      f"m/cell {r['m_per_cell']}  verify {len(r['verify'])}  "
      f"coverage {sum(len(v) for v in r['coverage'].values())}")
EOF
    echo "    output: data/$S/profile.npz + data/$S/qa/"
    echo "    AWAITING VISUAL VERDICT — inspect walkable_overlay.png and reachable.png"
    # only when a human is watching: a fleet run would pop N Preview windows
    [ -t 1 ] && command -v open >/dev/null && open "data/$S/qa/walkable_overlay.png"
fi

# A map alone routes nothing: without a calibrated Atlas the app has no way to
# put a real product on it, so the store stays unpickable until this passes.
# It does not depend on the audit verdict, so a blocked audit must not skip it.
if [ -n "${PIPE_NO_BROWSER:-}" ]; then
    # Deferred, not blocked. PLACEMENT stays ok on purpose: nothing was tried
    # here, so the gate below reads the audit verdict alone. Saying "blocked"
    # for a stage nobody ran is the same lie in the other direction.
    echo "==> [6/6] placement deferred (PIPE_NO_BROWSER)"
    echo "    run it from the main checkout, where the browser profile lives:"
    echo "      python3 capture_atlas.py $S && python3 calibrate.py $S"
else
    echo "==> [6/6] Atlas capture + calibration (opens a browser)"
    PLACEMENT=blocked
    if "$PYTHON" capture_atlas.py "$S"; then
        OFFLINE=blocked
        "$PYTHON" calibrate.py "$S" && OFFLINE=ok
        if [ "$OFFLINE" = ok ] || "$PYTHON" - "$S" <<'EOF'
import json, sys
record = json.load(open(f"data/{sys.argv[1]}-atlas/calibration.json"))
failed = [
    name for name, gate in record["gates"].items()
    if not (gate.get("pass") if isinstance(gate, dict) else gate)
]
raise SystemExit(failed != ["margin"])
EOF
        then
            echo "    checking live shelf labels"
            "$PYTHON" calibrate.py "$S" --verify && PLACEMENT=ok
        fi
    fi
    if [ "$PLACEMENT" != ok ] &&
            [ -f "data/$S-atlas/calibration.json" ]; then
        echo "    calibration blocked — starting data-only repair agent"
        repair_try=0
        while [ "$repair_try" -lt 3 ]; do
            repair_try=$((repair_try + 1))
            { echo "Execute this calibration runbook for store $S:"; \
              echo; sed "s/<N>/$S/g" docs/calibration.md; } |
                $AGENT > "data/$S-atlas/repair.log" 2>&1 && break
            grep -Eiq "api error|connection (closed|error)|timed? out|overloaded" \
                "data/$S-atlas/repair.log" || break
            [ "$repair_try" -ge 3 ] || sleep 60
        done
        if "$PYTHON" calibrate.py "$S" &&
                "$PYTHON" calibrate.py "$S" --verify; then
            PLACEMENT=ok
        fi
    fi
    if [ "$PLACEMENT" = ok ]; then
        echo "    store $S places products exactly"
    else
        # Exit non-zero: the store is not routable, and a run that reports
        # success here is the same lie as a silent one — the app would list it
        # as blocked while the dialog said it finished.
        echo "    the map is done; the placement layer is not."
    fi
fi

# Name what is actually wrong. Both can be, and each has its own way back in.
[ "$VERDICT" = clean ] || {
    echo "==> store $S is BLOCKED on its audit — read $LOG/audit.log, fix the"
    echo "    findings in data/$S/*.json, then: ./pipeline.sh $S --from 4"
}
[ "$PLACEMENT" = ok ] || {
    echo "==> store $S has a map but cannot place products — see"
    echo "    data/$S-atlas/calibration.json for the gate that failed"
}
[ "$VERDICT" = clean ] && [ "$PLACEMENT" = ok ]
