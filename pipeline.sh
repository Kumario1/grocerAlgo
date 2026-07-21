#!/bin/sh
# Autonomous store-onboarding pipeline: unknown H-E-B store number in,
# converged + audited map out.
#
#   ./pipeline.sh <store> [city-slug]        full run (agents included)
#   ./pipeline.sh <store> --no-agents        mechanical stages only
#
# Stages:
#   1. discover  — find + download + validate the store's guide PDF
#   2. rebuild   — extract -> profile -> QA (first pass may fail: that
#                  failure text is the onboarding agent's first input)
#   3. onboard   — headless agent runs docs/onboarding.md (edits only
#                  data/<store>/*.json truth files, loops to convergence)
#   4. audit     — a SEPARATE headless agent runs docs/audit.md
#                  adversarially; ships only on AUDIT CLEAN
#   5. output    — data/<store>/profile.npz + qa/report.json + PNGs
#
# Agent runner: claude -p (override: PIPE_AGENT='codex exec' etc.).
# Runs with --dangerously-skip-permissions — the guardrails live in the
# runbook prompts (data-files-only, no code, no goldens) and the golden +
# test gates catch violations. Run from a terminal, not from inside
# another agent session.
set -e
S=$1
[ -n "$S" ] || { echo "usage: ./pipeline.sh <store> [city-slug|--no-agents]"; exit 2; }
ARG2=$2
AGENT=${PIPE_AGENT:-"claude --dangerously-skip-permissions -p"}
LOG="data/$S/qa"

echo "==> [1/5] discover: store $S"
if [ "$ARG2" = "--no-agents" ] || [ -z "$ARG2" ]; then
    python3 discover.py "$S"
else
    python3 discover.py "$S" "$ARG2"
fi

echo "==> [2/5] first mechanical pass"
mkdir -p "$LOG"
if ./rebuild.sh "$S" > "$LOG/first_pass.log" 2>&1; then
    echo "    first pass clean (see $LOG/first_pass.log)"
else
    echo "    first pass exit $? — expected before truth exists; the"
    echo "    onboarding agent starts from $LOG/first_pass.log"
fi

if [ "$ARG2" = "--no-agents" ]; then
    echo "==> --no-agents: stopping after mechanical stages"
    exit 0
fi

echo "==> [3/5] onboarding agent (docs/onboarding.md, store $S)"
{ echo "You are in the grocerAlgo repo. Execute this runbook for store $S:"; \
  echo; sed "s/<N>/$S/g" docs/onboarding.md; } | $AGENT > "$LOG/onboard.log" 2>&1 \
    || { echo "onboarding agent failed — read $LOG/onboard.log"; exit 1; }
echo "    onboarding agent done ($LOG/onboard.log)"

./rebuild.sh "$S" > "$LOG/post_onboard.log" 2>&1 \
    || { echo "rebuild after onboarding failed — read $LOG/post_onboard.log"; exit 1; }

echo "==> [4/5] audit agent (docs/audit.md, store $S — fresh context)"
{ echo "You are the adversarial auditor in the grocerAlgo repo. Execute this runbook for store $S:"; \
  echo; sed "s/<N>/$S/g" docs/audit.md; } | $AGENT > "$LOG/audit.log" 2>&1 \
    || { echo "audit agent failed — read $LOG/audit.log"; exit 1; }

./rebuild.sh "$S" > "$LOG/post_audit.log" 2>&1 \
    || { echo "rebuild after audit failed — read $LOG/post_audit.log"; exit 1; }

echo "==> [5/5] verdict + output"
if grep -q "AUDIT CLEAN" "$LOG/audit.log"; then
    echo "    AUDIT CLEAN — store $S onboarded"
else
    echo "    audit did NOT report clean — findings in $LOG/audit.log:"
    grep -A2 "AUDIT FAILED" "$LOG/audit.log" || tail -20 "$LOG/audit.log"
    exit 1
fi
python3 - "$S" <<'EOF'
import json, sys
r = json.load(open(f"data/{sys.argv[1]}/qa/report.json"))
print(f"    walkable {r['walkable_pct']}%  reachable {r['reachable_pct']}%  "
      f"m/cell {r['m_per_cell']}  verify {len(r['verify'])}  "
      f"coverage {sum(len(v) for v in r['coverage'].values())}")
EOF
echo "    output: data/$S/profile.npz + data/$S/qa/"
command -v open >/dev/null && open "data/$S/qa/walkable_overlay.png"
