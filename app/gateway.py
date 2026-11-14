"""Ozon account isolation: one long-lived MCP stdio subprocess per member.

Never expose this HTTP listener publicly. It requires an internal bearer token,
and each subprocess has its own browser profile, auth backup and state files.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import closing
import logging
import os
import secrets
import sqlite3
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.mcp_client import unwrap_result

logger=logging.getLogger("ozon.gateway")
ALLOWED={"session_status","get_cart","list_orders","order_products","product_details",
         "start_login","submit_login_code"}
BASE=Path(os.environ.get("OZON_USERS_ROOT","/data/ozon/users"))
IDLE_SECONDS=240
MEMBERS_DB=os.environ.get("MEMBERS_DB","/botdata/watcher.sqlite3")


def active_member(user_id:int) -> bool:
    """Second authorization barrier independent of Telegram message handlers."""
    try:
        with closing(sqlite3.connect(f"file:{MEMBERS_DB}?mode=ro",uri=True,timeout=4)) as conn:
            return conn.execute("SELECT 1 FROM users WHERE user_id=? AND revoked=0",(user_id,)).fetchone() is not None
    except sqlite3.Error:
        return False



def check_uid(raw: Any) -> int:
    if type(raw) is not int or not 0 < raw < 2**63:
        raise ValueError("Invalid user ID")
    return raw


def env_for_user(user_id: int, base: Path = BASE) -> dict[str,str]:
    uid=check_uid(user_id)
    home=base/str(uid)
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(home,0o700)
    allowed_env={k:os.environ[k] for k in ("PATH","HOME","DISPLAY","LANG","PYTHONPATH","PLAYWRIGHT_BROWSERS_PATH","SSL_CERT_FILE") if k in os.environ}
    return {**allowed_env,
            "OZON_TRANSPORT":"stdio","OZON_ENABLE_WRITES":"0","OZON_ENABLE_ORDERS":"0",
            "OZON_PROFILE_DIR":str(home/"profile"),"OZON_PROFILE_BACKUP":str(home/"profile.backup"),
            "OZON_STATE_PATH":str(home/"state.json"),"OZON_MONITOR_STORE":str(home/"price_history.json")}


@dataclass
class Request:
    tool: str
    arguments: dict[str,Any]
    future: asyncio.Future


class UserWorker:
    def __init__(self, user_id: int, base: Path):
        self.user_id=user_id
        self.base=base
        self.queue:asyncio.Queue[Request]=asyncio.Queue()
        self.task=asyncio.create_task(self._loop(), name=f"ozon-user-{user_id}")

    async def _loop(self) -> None:
        try:
            parameters=StdioServerParameters(command="/app/.venv/bin/python",args=["-m","ozon_mcp"],
                                              env=env_for_user(self.user_id,self.base))
            # Scope entry/exit stay in this *same* task (AnyIO's CancelScope requirement).
            async with stdio_client(parameters) as (reader,writer):
                async with ClientSession(reader,writer) as client:
                    await client.initialize()
                    while True:
                        try:
                            req=await asyncio.wait_for(self.queue.get(),timeout=IDLE_SECONDS)
                        except asyncio.TimeoutError:
                            break
                        if req.tool=="__shutdown__":
                            if not req.future.done():req.future.set_result(None)
                            break
                        try:
                            answer=unwrap_result(await client.call_tool(req.tool,arguments=req.arguments))
                        except Exception as exc:
                            logger.warning("MCP call failed user=%s tool=%s type=%s",self.user_id,req.tool,type(exc).__name__)
                            if not req.future.done():req.future.set_exception(RuntimeError("Ozon отклонил запрос или сессия истекла"))
                        else:
                            if not req.future.done():req.future.set_result(answer)
        except BaseException as exc:
            if not isinstance(exc,asyncio.CancelledError):
                logger.error("User subprocess stopped user=%s reason=%s",self.user_id,type(exc).__name__)
            # Callers receive an error via the pending queue and can retry.
        finally:
            while not self.queue.empty():
                req=self.queue.get_nowait()
                if not req.future.done():req.future.set_exception(RuntimeError("Ozon worker stopped"))

    async def call(self, tool: str, arguments: dict[str,Any]) -> Any:
        future=asyncio.get_running_loop().create_future()
        await self.queue.put(Request(tool,arguments,future))
        return await asyncio.wait_for(future,timeout=195)

    async def stop(self) -> None:
        if not self.task.done():
            fut=asyncio.get_running_loop().create_future()
            await self.queue.put(Request("__shutdown__",{},fut))
            try: await asyncio.wait_for(self.task,timeout=40)
            except (TimeoutError,Exception): self.task.cancel()
        with contextlib.suppress(BaseException):await self.task


class WorkerManager:
    def __init__(self,base:Path=BASE):
        self.base=base
        self.workers:dict[int,UserWorker]={}
        self.lock=asyncio.Lock()

    async def call(self,user_id:int,tool:str,arguments:dict[str,Any]) -> Any:
        check_uid(user_id)
        if tool not in ALLOWED:
            raise ValueError("Operation is not allowed")
        if not active_member(user_id):
            raise ValueError("User is not registered or was revoked")
        async with self.lock:
            old=self.workers.get(user_id)
            if old is None or old.task.done():
                self.workers[user_id]=UserWorker(user_id,self.base)
            worker=self.workers[user_id]
        return await worker.call(tool,arguments)

    async def drop(self,user_id:int) -> None:
        check_uid(user_id)
        async with self.lock:
            worker=self.workers.pop(user_id,None)
            if worker:await worker.stop()
            target=self.base/str(user_id)
            if target.exists(): shutil.rmtree(target)

    async def close(self) -> None:
        async with self.lock:
            for worker in self.workers.values():await worker.stop()
            self.workers.clear()


def gateway_app(manager: WorkerManager | None = None, token: str | None = None) -> web.Application:
    mgr=manager or WorkerManager()
    secret=token if token is not None else os.environ.get("GATEWAY_TOKEN","")
    if len(secret)<32:
        raise RuntimeError("GATEWAY_TOKEN must be at least 32 characters")

    @web.middleware
    async def authenticated(request:web.Request,handler):
        supplied=request.headers.get("Authorization","")
        if not secrets.compare_digest(supplied,"Bearer "+secret):
            raise web.HTTPUnauthorized()
        if request.content_length is not None and request.content_length>4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096,actual_size=request.content_length)
        return await handler(request)

    app=web.Application(middlewares=[authenticated],client_max_size=4096)

    async def call(request:web.Request):
        try:
            obj=await request.json()
            user_id=check_uid(obj.get("user_id"))
            tool=obj.get("tool")
            args=obj.get("arguments",{})
            if tool not in ALLOWED or not isinstance(args,dict): raise ValueError("Invalid method")
            result=await mgr.call(user_id,tool,args)
            return web.json_response({"result":result})
        except (ValueError,RuntimeError,TimeoutError) as exc:
            return web.json_response({"error":str(exc)[:120]},status=400 if isinstance(exc,ValueError) else 503)

    async def drop(request:web.Request):
        try:
            obj=await request.json()
            await mgr.drop(check_uid(obj.get("user_id")))
            return web.json_response({"result":True})
        except ValueError:
            return web.json_response({"error":"Invalid user ID"},status=400)

    async def cleanup(_app:web.Application):await mgr.close()
    app.router.add_post("/api/call",call)
    app.router.add_post("/api/drop",drop)
    app.on_cleanup.append(cleanup)
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    web.run_app(gateway_app(),host="0.0.0.0",port=8085,access_log=None)

if __name__=="__main__":main()
