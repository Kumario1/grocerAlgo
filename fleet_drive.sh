#!/bin/sh
# Compat wrapper — driver lives in scripts/.
exec "$(CDPATH= cd -- "$(dirname "$0")" && pwd)/scripts/fleet_drive.sh" "$@"
