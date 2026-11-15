#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo 'Created .env — set TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID.'
fi
ozon_ref="$(tr -d '\r\n' < deploy/ozon-mcp.ref)"
if [[ ! "$ozon_ref" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Invalid pinned Ozon MCP commit in deploy/ozon-mcp.ref.' >&2
  exit 1
fi
fresh_clone=0
if [ ! -d vendor/ozon-mcp/.git ]; then
  if [ -e vendor/ozon-mcp ]; then
    echo 'Cannot verify vendor/ozon-mcp: preserve/move that directory and rerun prepare.sh.' >&2
    exit 1
  fi
  mkdir -p vendor
  git clone --no-checkout --depth 1 https://github.com/Alexander-Zhukov/ozon-mcp.git vendor/ozon-mcp
  fresh_clone=1
fi
if [[ "$fresh_clone" == 0 && -n "$(git -C vendor/ozon-mcp status --porcelain --untracked-files=normal)" ]]; then
  echo 'Local changes in vendor/ozon-mcp: preserve them before updating the pinned version.' >&2
  exit 1
fi
if ! git -C vendor/ozon-mcp cat-file -e "$ozon_ref^{commit}" 2>/dev/null; then
  git -C vendor/ozon-mcp fetch --depth 1 origin "$ozon_ref"
fi
git -C vendor/ozon-mcp -c advice.detachedHead=false checkout --detach "$ozon_ref"
mkdir -p data/ozon/users data/bot data/awg-client/wg_confs
# Migrate the original single-user browser profile under the owner-specific path.
owner_id="$(sed -n 's/^TELEGRAM_USER_ID=//p' .env | head -n 1)"
if [[ "$owner_id" =~ ^[0-9]+$ ]] && [ -d data/ozon/profile ] && [ ! -e "data/ozon/users/$owner_id" ]; then
  mkdir -p "data/ozon/users/$owner_id"
  mv data/ozon/profile "data/ozon/users/$owner_id/profile"
  for p in profile.backup state.json price_history.json; do
    [ ! -e "data/ozon/$p" ] || mv "data/ozon/$p" "data/ozon/users/$owner_id/$p"
  done
  echo 'Old single-owner Ozon profile moved to its private user directory.'
fi
chmod 700 data/awg-client data/awg-client/wg_confs
chmod 700 data/ozon data/bot
# The bot container runs as uid 10001 and needs write access to SQLite.
if command -v chown >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then chown -R 10001:10001 data/bot; fi
printf 'Prepared. Add your AWG Router client profile at data/awg-client/wg_confs/awg0.conf (chmod 600).\nNext: docker compose up -d --build\n'
