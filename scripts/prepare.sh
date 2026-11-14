#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo 'Created .env — set TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID.'
fi
if [ ! -f vendor/ozon-mcp/Dockerfile ]; then
  mkdir -p vendor
  git clone --depth 1 https://github.com/Alexander-Zhukov/ozon-mcp.git vendor/ozon-mcp
fi
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
if command -v chown >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then chown 10001:10001 data/bot; fi
printf 'Prepared. Add your AWG Router client profile at data/awg-client/wg_confs/awg0.conf (chmod 600).\nNext: docker compose up -d --build\n'
