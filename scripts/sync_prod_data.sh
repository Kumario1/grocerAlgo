#!/bin/sh
# Regenerate .dockerignore from passing calibrations. See sync_prod_data.py.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PYTHON="${PIPE_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3
exec "$PYTHON" "$ROOT/scripts/sync_prod_data.py" "$@"
