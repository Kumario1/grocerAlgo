#!/bin/sh
# Promote catalog-ready stores to PROD: commit data, regen .dockerignore,
# git push (Railway rebuild), bootstrap + push HEB states, optional smoke.
#
#   ./scripts/promote_stores.sh 123 456   # explicit ids
#   ./scripts/promote_stores.sh           # drain logs/promote_queue
#
# Env:
#   GROCER_PROD_URL, GROCER_ADMIN_TOKEN  — required unless --data-only
#   PROMOTE_SMOKE=1                      — one search+locate per store on PROD
#   PROMOTE_SKIP_STATES=1                — ship data only (explicit escape hatch)
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PIPE_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3
QUEUE="${PROMOTE_QUEUE:-$ROOT/logs/promote_queue}"
SKIP_STATES="${PROMOTE_SKIP_STATES:-0}"
SMOKE="${PROMOTE_SMOKE:-0}"
DATA_ONLY=0

usage() {
    awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-states) SKIP_STATES=1; shift ;;
        --data-only) DATA_ONLY=1; SKIP_STATES=1; shift ;;
        -h|--help) usage ;;
        --) shift; break ;;
        -*) echo "unknown flag: $1" >&2; usage ;;
        *) break ;;
    esac
done

mkdir -p "$(dirname "$QUEUE")"
touch "$QUEUE"

ids=
if [ $# -gt 0 ]; then
    ids=$*
else
    ids=$(tr -s '[:space:]' '\n' < "$QUEUE" | sed '/^$/d' | sort -nu)
fi

if [ -z "${ids:-}" ]; then
    echo "promote_stores: nothing to promote" >&2
    exit 0
fi

catalog_ok() {
    "$PYTHON" -c "
from router.calibrate import is_catalog_enabled
import sys
sys.exit(0 if is_catalog_enabled(sys.argv[1]) else 1)
" "$1"
}

queue_remove() {
    s=$1
    [ -f "$QUEUE" ] || return 0
    tmp=$(mktemp)
    tr -s '[:space:]' '\n' < "$QUEUE" | sed '/^$/d' | grep -vx "$s" > "$tmp" || true
    mv "$tmp" "$QUEUE"
}

queue_keep() {
    s=$1
    grep -qx "$s" "$QUEUE" 2>/dev/null && return 0
    echo "$s" >> "$QUEUE"
}

commit_store() {
    s=$1
    paths=
    [ -d "data/$s" ] && { git add -f -- "data/$s"; paths="$paths data/$s"; }
    [ -d "data/$s-atlas" ] && {
        git add -f -- "data/$s-atlas"
        paths="$paths data/$s-atlas"
    }
    for pdf in guides/guide-*-"$s".pdf; do
        [ -f "$pdf" ] || continue
        git add -f -- "$pdf"
        paths="$paths $pdf"
    done
    [ -n "$paths" ] || return 0
    # shellcheck disable=SC2086
    git commit -q -m "feat(data): promote store $s to prod" -- $paths 2>/dev/null \
        || true
}

smoke_store() {
    s=$1
    url="${GROCER_PROD_URL%/}"
    search=$(curl -fsS "$url/api/products?store=$s&q=milk" | "$PYTHON" -c "
import json,sys
body=json.load(sys.stdin)
prods=body.get('products') or []
print(prods[0]['id'] if prods else '')
")
    [ -n "$search" ] || {
        echo "smoke $s: FAIL — search returned no products" >&2
        return 1
    }
    curl -fsS -X POST "$url/api/products/locate?store=$s" \
        -H 'Content-Type: application/json' \
        -d "{\"products\":[{\"id\":\"$search\",\"name\":\"milk\"}]}" >/dev/null
    echo "smoke $s: ok"
}

failed=
pending=
for s in $ids; do
    if ! catalog_ok "$s"; then
        echo "promote $s: SKIP — not catalog-enabled (pass calibration required)" >&2
        queue_keep "$s"
        failed="$failed $s"
        continue
    fi
    echo "promote $s: commit data"
    commit_store "$s"
    pending="$pending $s"
done

if [ -n "${pending:-}" ]; then
    ./scripts/sync_prod_data.sh
    if git status --porcelain -- .dockerignore | grep -q .; then
        git add -- .dockerignore
        git commit -q -m "chore: sync .dockerignore for catalog stores" -- .dockerignore \
            || true
    fi
fi

if [ "$DATA_ONLY" != 1 ] && [ -n "${pending:-}" ]; then
    if [ -z "${GROCER_PROD_URL:-}" ] || [ -z "${GROCER_ADMIN_TOKEN:-}" ]; then
        echo "promote_stores: GROCER_PROD_URL and GROCER_ADMIN_TOKEN required" >&2
        echo "  (data commits done; queue left intact — re-run with secrets)" >&2
        exit 1
    fi
    echo "promote_stores: git push"
    git push
fi

for s in $pending; do
    if [ "$SKIP_STATES" = 1 ] || [ "$DATA_ONLY" = 1 ]; then
        echo "promote $s: skip states"
        queue_remove "$s"
        continue
    fi
    echo "promote $s: bootstrap local state"
    if ! "$PYTHON" -m scripts.bootstrap_heb_states "$s"; then
        echo "promote $s: FAIL bootstrap" >&2
        queue_keep "$s"
        failed="$failed $s"
        continue
    fi
    echo "promote $s: push state to PROD"
    if ! ./scripts/push_heb_states.sh --wait-catalog "$s"; then
        echo "promote $s: FAIL push states" >&2
        queue_keep "$s"
        failed="$failed $s"
        continue
    fi
    if [ "$SMOKE" = 1 ]; then
        if ! smoke_store "$s"; then
            queue_keep "$s"
            failed="$failed $s"
            continue
        fi
    fi
    queue_remove "$s"
    echo "promote $s: DONE"
done

if [ -n "${failed:-}" ]; then
    echo "promote_stores: failed:$failed" >&2
    exit 1
fi
echo "promote_stores: all ok"
