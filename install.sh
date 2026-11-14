#!/usr/bin/env bash
# Run this in an interactive SSH session on the Docker host.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--no-start" ) ]]; then
  echo 'Usage: ./install.sh [--no-start]' >&2
  exit 2
fi
if [ ! -t 0 ]; then
  echo 'Required: interactive terminal (ssh -t), not a pipe.' >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Install python3 first.' >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo 'Install Docker Engine and Docker Compose plugin first.' >&2
  exit 2
fi
python3 scripts/configure.py
./scripts/prepare.sh
docker compose config --quiet
if [[ "${1:-}" == "--no-start" ]]; then
  echo 'Configuration ready. Run: docker compose up -d --build'
  exit 0
fi
echo 'Building and starting the isolated Docker services...'
# Only start public HTTPS if ports are free: do not interrupt existing AWG-Router/Caddy.
login_domain="$(sed -n 's/^LOGIN_DOMAIN=//p' .env | head -n 1)"
if [[ -n "$login_domain" ]] && command -v ss >/dev/null && ! (ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)(80|443)$'); then
  echo 'No 80/443 listeners found. Starting family HTTPS reverse proxy.'
  docker compose --profile family-https up -d --build
else
  docker compose up -d --build
  if [[ -n "$login_domain" ]]; then
    echo "WARNING: host 80/443 in use. Existing reverse proxy must forward https://$login_domain to http://127.0.0.1:8765"
    echo 'Do not open port 8765 publicly (HTTP, loopback only).'
  else
    echo 'No login domain yet. Family /connect disabled; set HTTPS domain and rerun install.'
  fi
fi
printf '\n=== Route diagnostics ===\n'
if ! ./scripts/check_network.sh; then
  echo 'WARNING: Network test failed. Check logs and AWG-Router policy before sending /sync.' >&2
  exit 1
fi
cat <<'TXT'

Installation finished. In Telegram:
  /invite    — give a family member a private one-use link
  /connect   — each user connects their own Ozon via the HTTPS website
  /status    — check each account
  /sync      — monitor that user's cart and orders

No SMS codes or Ozon credentials should be sent to Telegram.
TXT
