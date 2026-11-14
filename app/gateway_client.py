"""Authenticated internal-only HTTP adapter to per-user Ozon MCP processes."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiohttp


class GatewayError(RuntimeError):
    pass


class GatewayClient:
    def __init__(self, session: aiohttp.ClientSession, base: str, token: str, user_id: int):
        self.session = session
        self.base = base.rstrip("/")
        self.token = token
        self.user_id = user_id

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        headers={"Authorization":f"Bearer {self.token}"}
        try:
            async with self.session.post(self.base+path,json=payload,headers=headers) as res:
                try:
                    result=await res.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError) as exc:
                    raise GatewayError("Ozon gateway returned an invalid response") from exc
                if res.status != 200:
                    # Never leak server traceback or credentials to Telegram.
                    raise GatewayError(str(result.get("error", "Ozon service unavailable"))[:180])
                return result.get("result")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise GatewayError("Нет соединения с внутренним сервисом Ozon") from exc

    async def call(self, tool: str, **kwargs: Any) -> Any:
        return await self._post("/api/call",{"user_id":self.user_id,"tool":tool,"arguments":kwargs})

    async def drop_account(self) -> Any:
        return await self._post("/api/drop", {"user_id":self.user_id})


@asynccontextmanager
async def connect_gateway(url: str, token: str, user_id: int) -> AsyncIterator[GatewayClient]:
    timeout=aiohttp.ClientTimeout(total=210)
    async with aiohttp.ClientSession(timeout=timeout,trust_env=False) as session:
        yield GatewayClient(session,url,token,user_id)
