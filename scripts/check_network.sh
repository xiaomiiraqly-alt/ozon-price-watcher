#!/usr/bin/env bash
# Validate that Telegram exits via AWG Router/upstream and Ozon exits directly.
# Does not expose Telegram bot token or Ozon session credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

echo '=== AWG client tunnel ==='
docker compose exec -T awg-client awg show awg0

echo '=== Public exit seen by Telegram SOCKS5 ==='
docker compose exec -T bot python - <<'PY'
import asyncio
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
            print('Telegram VPN exit:', (await response.text()).strip())
        async with session.get('https://api.telegram.org') as response:
            # 200/302/404 are all HTTP reachable; the endpoint needs no token here.
            print('Telegram HTTPS status:', response.status)
asyncio.run(main())
PY

echo '=== Public exit seen by Ozon gateway (no proxy) ==='
docker compose exec -T ozon-gateway /app/.venv/bin/python - <<'PY'
import urllib.request
with urllib.request.urlopen('https://api.ipify.org', timeout=15) as response:
    print('Ozon direct exit:', response.read().decode().strip())
PY
