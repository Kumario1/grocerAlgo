#!/bin/sh
# Railway / Docker entrypoint: bring up a real X display, then uvicorn.
set -eu

DISPLAY_NUM="${DISPLAY#:}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"

Xvfb "${DISPLAY}" -screen 0 1280x720x24 -nolisten tcp &
XVFB_PID=$!

i=0
while [ "$i" -lt 40 ]; do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${XVFB_PID}" 2>/dev/null; then
    echo "Xvfb exited before the display came up" >&2
    exit 1
  fi
  i=$((i + 1))
  sleep 0.25
done

if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
  echo "Xvfb display ${DISPLAY} never became ready" >&2
  exit 1
fi

# Stale Chrome profile locks on the Railway volume kill the next CDP launch.
RUNTIME_DIR="${HEB_RUNTIME_DIR:-/app/runtime}"
CHROME_DIR="${RUNTIME_DIR}/chrome"
mkdir -p "${CHROME_DIR}"
rm -f \
  "${CHROME_DIR}/SingletonLock" \
  "${CHROME_DIR}/SingletonSocket" \
  "${CHROME_DIR}/SingletonCookie"

exec python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
