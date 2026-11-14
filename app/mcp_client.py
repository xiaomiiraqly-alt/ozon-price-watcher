"""Minimal read-only MCP adapter for Alexander-Zhukov/ozon-mcp."""
from __future__ import annotations

import json
from datetime import timedelta
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class OzonMCPError(RuntimeError):
    pass


def unwrap_result(response: Any) -> Any:
    if response.isError:
        details = " ".join(getattr(entry, "text", "") for entry in response.content)
        raise OzonMCPError(details[:500] or "Ozon MCP returned an error")
    data = getattr(response, "structuredContent", None)
    if data is not None:
        if isinstance(data, dict) and set(data) == {"result"}:
            return data["result"]
        return data
    content = [getattr(entry, "text", None) for entry in response.content]
    texts = [s for s in content if isinstance(s, str)]
    if not texts:
        raise OzonMCPError("MCP returned no readable content")
    try:
        return json.loads(texts[0])
    except json.JSONDecodeError as exc:
        raise OzonMCPError("MCP returned malformed JSON") from exc


class OzonSession:
    def __init__(self, client: ClientSession):
        self._client = client

    async def call(self, tool: str, **kwargs: Any) -> Any:
        # Strictly limit the bot to read-only tools. Login is performed via app.auth.
        if tool not in ("session_status", "get_cart", "list_orders", "order_products", "product_details"):
            raise OzonMCPError(f"Tool {tool} is not available in the monitoring process")
        return unwrap_result(await self._client.call_tool(tool, arguments=kwargs))


@asynccontextmanager
async def connect_mcp(url: str) -> AsyncIterator[OzonSession]:
    async with streamablehttp_client(url, timeout=timedelta(seconds=120)) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield OzonSession(client)


@asynccontextmanager
async def connect_auth_mcp(url: str) -> AsyncIterator[ClientSession]:
    async with streamablehttp_client(url, timeout=timedelta(seconds=120)) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client
