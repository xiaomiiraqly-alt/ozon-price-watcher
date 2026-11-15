#!/usr/bin/env bash
# Validate that Telegram exits via AWG Router/upstream and Ozon exits directly.
# Does not expose Telegram bot token or Ozon session credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

echo '=== AWG client tunnel ==='
docker compose exec -T awg-client awg show awg0

echo '=== Public exit seen by Telegram SOCKS5 ==='
telegram_exit="$(docker compose exec -T bot python - <<'PY'
import asyncio
import ipaddress
import os
from aiohttp import ClientSession, ClientTimeout
from aiohttp_socks import ProxyConnector

async def main():
    proxy = os.environ['TELEGRAM_PROXY_URL']
    async with ClientSession(
        connector=ProxyConnector.from_url(proxy),
        timeout=ClientTimeout(total=20),
    ) as session:
        async with session.get('https://api.ipify.org') as response:
            response.raise_for_status()
            exit_ip=str(ipaddress.IPv4Address((await response.text()).strip()))
        async with session.get('https://api.telegram.org') as response:
            # 200/302/404 are all HTTP reachable; the endpoint needs no token here.
            if response.status>=400 and response.status!=404:
                response.raise_for_status()
        print(exit_ip)
asyncio.run(main())
PY
)"
printf 'Telegram VPN exit: %s\n' "$telegram_exit"

echo '=== Public exit seen by Ozon gateway (no proxy) ==='
ozon_exit="$(docker compose exec -T ozon-gateway /app/.venv/bin/python - <<'PY'
import ipaddress
import urllib.request
with urllib.request.urlopen('https://api.ipify.org', timeout=15) as response:
    print(ipaddress.IPv4Address(response.read().decode().strip()))
PY
)"
printf 'Ozon direct exit: %s\n' "$ozon_exit"
if [[ "$telegram_exit" == "$ozon_exit" ]]; then
  echo 'ERROR: Telegram and Ozon use the same exit IP. Check the AWG Router overseas policy.' >&2
  exit 1
fi
