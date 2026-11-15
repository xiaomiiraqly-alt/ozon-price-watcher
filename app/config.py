"""Server-side configuration. Never print connection secrets."""
from __future__ import annotations

import os
from urllib.parse import urlsplit
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    telegram_proxy_url: str
    owner_id: int
    gateway_url: str
    gateway_token: str
    login_base_url: str
    db_path: str
    interval: int
    price_mode: str

    @classmethod
    def from_env(cls) -> "Config":
        token=os.environ.get("TELEGRAM_BOT_TOKEN","").strip()
        owner=os.environ.get("TELEGRAM_USER_ID","").strip()
        if not token or not owner.isascii() or not owner.isdigit() or len(owner)>19 or not 0<int(owner)<2**63:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and numeric TELEGRAM_USER_ID in .env")
        proxy=os.environ.get("TELEGRAM_PROXY_URL","").strip()
        proxy_parts=urlsplit(proxy)
        if proxy_parts.scheme!="socks5" or not proxy_parts.hostname or not proxy_parts.port:
            raise ValueError("TELEGRAM_PROXY_URL must be socks5://awg-client:1080; direct Telegram is forbidden")
        mode=os.environ.get("PRICE_MODE","bank").strip()
        if mode not in ("bank","regular"):
            raise ValueError("PRICE_MODE must be bank or regular")
        secret=os.environ.get("GATEWAY_TOKEN","").strip()
        if len(secret)<32:
            raise ValueError("GATEWAY_TOKEN must contain at least 32 random characters")
        base=os.environ.get("LOGIN_BASE_URL","").strip().rstrip("/")
        if base:
            parsed=urlsplit(base)
            if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
                raise ValueError("LOGIN_BASE_URL must be an HTTPS origin without a path")
        return cls(bot_token=token,telegram_proxy_url=proxy,owner_id=int(owner),
                   gateway_url=os.environ.get("OZON_GATEWAY_URL","http://ozon-gateway:8085"),
                   gateway_token=secret,login_base_url=base,
                   db_path=os.environ.get("DATABASE_PATH","/data/watcher.sqlite3"),
                   interval=max(600,int(os.environ.get("CHECK_INTERVAL_SECONDS","1800"))),price_mode=mode)
