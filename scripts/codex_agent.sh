#!/bin/sh
# Run one fleet stage with Codex, resuming the same session after capacity blips.
set -u

WAIT=${PIPE_CODEX_CAPACITY_WAIT:-300}
case $WAIT in
    ''|*[!0-9]*) echo "PIPE_CODEX_CAPACITY_WAIT must be seconds"; exit 2 ;;
esac

STATUS=$(mktemp "${TMPDIR:-/tmp}/grocer-codex-status.XXXXXX")
TRANSCRIPT=$(mktemp "${TMPDIR:-/tmp}/grocer-codex-log.XXXXXX")
cleanup() { rm -f "$STATUS" "$TRANSCRIPT"; }
trap cleanup 0 HUP INT TERM

run_codex() {
    : > "$STATUS"
    : > "$TRANSCRIPT"
    (
        "$@"
        echo $? > "$STATUS"
    ) 2>&1 | tee "$TRANSCRIPT"
    [ -s "$STATUS" ] && RC=$(cat "$STATUS") || RC=1
    return "$RC"
}

run_codex codex exec --ignore-user-config --disable multi_agent \
    --sandbox workspace-write --model gpt-5.6-luna \
    -c model_reasoning_effort=max - && exit 0

SESSION=$(sed -n 's/^session id: //p' "$TRANSCRIPT" | tail -1)
[ -n "$SESSION" ] || exit "$RC"
while grep -Eiq 'selected model is at capacity' "$TRANSCRIPT"; do
    echo "Codex model at capacity; resuming session $SESSION in $WAIT seconds"
    sleep "$WAIT"
    run_codex codex exec resume "$SESSION" --ignore-user-config \
        --disable multi_agent --model gpt-5.6-luna \
        -c model_reasoning_effort=max \
        "Continue exactly where you stopped and complete the runbook." &&
        exit 0
done
exit "$RC"
