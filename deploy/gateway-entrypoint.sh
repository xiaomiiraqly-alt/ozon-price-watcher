#!/usr/bin/env bash
# Headed Chromium is required by Ozon; one display shared by isolated processes.
set -euo pipefail
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1366x900x24 >/tmp/ozon-xvfb.log 2>&1 &
export DISPLAY=:99
for _ in $(seq 1 30); do
  [ -e /tmp/.X11-unix/X99 ] && break
  sleep 0.5
done
if [ ! -e /tmp/.X11-unix/X99 ]; then
  echo 'Xvfb failed' >&2
  exit 1
fi
fluxbox >/tmp/ozon-fluxbox.log 2>&1 &
exec /app/.venv/bin/python -m app.gateway
