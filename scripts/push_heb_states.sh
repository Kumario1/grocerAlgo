#!/bin/sh
# Push local verified H-E-B states to PROD. See push_heb_states.py.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="${PIPE_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3
cd "$ROOT"
exec "$PYTHON" -m scripts.push_heb_states "$@"
