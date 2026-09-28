#!/bin/sh
# Compat wrapper — onboard helper lives in scripts/.
exec "$(CDPATH= cd -- "$(dirname "$0")" && pwd)/scripts/onboard_fleet.sh" "$@"
