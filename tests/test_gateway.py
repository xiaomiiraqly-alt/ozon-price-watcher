"""Exercise real HTTP validation and worker lifecycle without an Ozon account."""
import asyncio
import os
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from mcp import StdioServerParameters

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.gateway import UserWorker, WorkerManager, gateway_app, safe_mcp_error_category
from app.mcp_client import OzonMCPError
from app.gateway_client import GatewayClient, GatewayError


class SafeMCPDiagnosticTests(unittest.TestCase):
    def test_categories_do_not_contain_account_data(self):
        private = "user@example.org phone=+79001234567 otp=654321"
        for message, category in (
            ("Variti challenge " + private, "antibot"),
            ("Playwright browser launch failed " + private, "browser"),
            ("429 too many requests " + private, "rate_limit"),
            ("An unexpected Ozon error " + private, "unknown_upstream_error"),
        ):
            with self.subTest(category=category):
                result = safe_mcp_error_category(OzonMCPError(message))
                self.assertEqual(result, category)
                self.assertNotIn("example.org", result)
                self.assertNotIn("654321", result)
        self.assertEqual(safe_mcp_error_category(ConnectionError(private)), "transport_or_runtime")


@asynccontextmanager
async def fake_stdio(parameters):
    yield None,None


class FakeSession:
    def __init__(self,*args):pass
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def initialize(self):pass
    async def call_tool(self,tool,arguments):
        return SimpleNamespace(isError=False,structuredContent={"signed_in":True},content=[])


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name,value in (("stdio_client",fake_stdio),("ClientSession",FakeSession)):
            patcher=patch("app.gateway."+name,value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_cancelled_worker_resolves_inflight_caller(self):
        started=asyncio.Event()
        async def blocked(*args,**kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch.object(FakeSession,"call_tool",blocked):
            worker=UserWorker(111,Path(self.tmp.name))
            call=asyncio.create_task(worker.call("session_status",{}))
            await started.wait()
            worker.task.cancel()
            with suppress(asyncio.CancelledError):await worker.task
            with self.assertRaisesRegex(RuntimeError,"worker stopped"):
                await asyncio.wait_for(call,.5)
            with self.assertRaises(RuntimeError):await worker.call("session_status",{})

    async def test_timed_out_call_retires_worker(self):
        async def blocked(*args,**kwargs):await asyncio.Event().wait()
        with patch.object(FakeSession,"call_tool",blocked),patch("app.gateway.CALL_TIMEOUT",.02):
            worker=UserWorker(111,Path(self.tmp.name))
            with self.assertRaisesRegex(RuntimeError,"timed out"):
                await asyncio.wait_for(worker.call("get_cart",{}),.5)
            await worker.stop()
            self.assertFalse(worker.accepting)

    async def test_transport_error_retires_worker(self):
        async def disconnected(*args,**kwargs):raise ConnectionError()
        with patch.object(FakeSession,"call_tool",disconnected):
            worker=UserWorker(111,Path(self.tmp.name))
            with self.assertRaises(RuntimeError):await worker.call("get_cart",{})
            await worker.stop()
            self.assertFalse(worker.accepting)

    async def test_pending_login_lives_longer_than_normal_idle_worker(self):
        with patch("app.gateway.IDLE_SECONDS",.02),patch("app.gateway.LOGIN_IDLE_SECONDS",.3):
            worker=UserWorker(111,Path(self.tmp.name))
            try:
                await worker.call("start_login",{})
                await asyncio.sleep(.06)
                self.assertFalse(worker.task.done())
                await worker.call("submit_login_code",{})
            finally:await worker.stop()

    async def test_cancelled_queued_request_is_never_executed(self):
        calls=[]
        async def record(self,tool,arguments):
            calls.append(tool)
            return SimpleNamespace(isError=False,structuredContent={},content=[])
        with patch.object(FakeSession,"call_tool",record):
            worker=UserWorker(111,Path(self.tmp.name))
            from app.gateway import Request
            expired=asyncio.get_running_loop().create_future()
            expired.cancel()
            worker.queue.put_nowait(Request("start_login",{},expired))
            await worker.call("session_status",{})
            await worker.stop()
            self.assertEqual(calls,["session_status"])

    async def test_drop_serializes_per_user_without_blocking_other_users(self):
        entered=asyncio.Event()
        release=asyncio.Event()
        original=FakeSession.call_tool
        async def block(self,tool,arguments):
            if tool=="get_cart":
                entered.set()
                await release.wait()
            return await original(self,tool,arguments)
        manager=WorkerManager(Path(self.tmp.name))
        with patch("app.gateway.active_member",return_value=True),patch.object(FakeSession,"call_tool",block):
            try:
                ongoing=asyncio.create_task(manager.call(111,"get_cart",{}))
                await entered.wait()
                deleting=asyncio.create_task(manager.drop(111))
                await asyncio.wait_for(manager.call(222,"session_status",{}),.5)
                self.assertFalse(deleting.done())
                release.set()
                await ongoing
                await deleting
                self.assertNotIn(111,manager.workers)
                self.assertFalse((Path(self.tmp.name)/"111").exists())
            finally:
                release.set()
                await manager.close()

    async def test_queued_request_deadline_includes_lock_wait(self):
        manager=WorkerManager(Path(self.tmp.name))
        with patch("app.gateway.REQUEST_TIMEOUT",.02),patch("app.gateway.active_member",return_value=True):
            async with manager.user_lock(111):
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(manager.call(111,"start_login",{}),.5)
            self.assertEqual(manager.workers,{})


class GatewayHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_requests_are_rejected_without_server_errors(self):
        client=TestClient(TestServer(gateway_app(token="a"*48)))
        await client.start_server()
        try:
            headers={"Authorization":"Bearer "+"a"*48}
            for path in ("/api/call","/api/drop"):
                for payload in ([],None,"text",42):
                    with self.subTest(path=path,payload=payload):
                        response=await client.post(path,json=payload,headers=headers)
                        self.assertEqual(response.status,400)
            for tool in ([],{},None,"place_order"):
                response=await client.post("/api/call",json={"user_id":111,"tool":tool},headers=headers)
                self.assertEqual(response.status,400)
            response=await client.post("/api/call",json={},headers={"Authorization":"Bearer é"})
            self.assertEqual(response.status,401)
        finally:await client.close()

    async def test_profile_deletion_error_is_safe_json(self):
        class Manager:
            async def drop(self,uid):raise OSError("private profile path")
            async def close(self):pass
        client=TestClient(TestServer(gateway_app(Manager(),token="a"*48)))
        await client.start_server()
        try:
            response=await client.post("/api/drop",json={"user_id":111},headers={"Authorization":"Bearer "+"a"*48})
            self.assertEqual(response.status,503)
            self.assertNotIn("private profile path",await response.text())
            self.assertIn("error",await response.json())
        finally:await client.close()

    async def test_client_rejects_malformed_success_responses(self):
        payload=[]
        async def reply(request):return web.json_response(payload)
        app=web.Application()
        app.router.add_post("/api/call",reply)
        async with TestServer(app) as server,aiohttp.ClientSession() as session:
            api=GatewayClient(session,str(server.make_url("/")),"a"*48,111)
            for payload in ([],None,{},"text"):
                with self.subTest(payload=payload),self.assertRaises(GatewayError):
                    await api.call("session_status")


class RealMCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_contract_and_process_shutdown(self):
        with TemporaryDirectory() as tmp:
            script=Path(tmp)/"server.py"
            script.write_text('''
import os
from mcp.server.fastmcp import FastMCP
server=FastMCP("Ozon contract fixture")
@server.tool()
def session_status() -> dict:
    return {"signed_in":True,"pid":os.getpid()}
@server.tool()
def list_orders() -> list[dict]:
    return [{"order_number":"111111-0001"}]
server.run(transport="stdio")
''')
            def parameters(**kwargs):
                return StdioServerParameters(command=sys.executable,args=[str(script)],env=kwargs["env"])
            with patch("app.gateway.StdioServerParameters",parameters):
                worker=UserWorker(111,Path(tmp)/"users")
                try:
                    status=await asyncio.wait_for(worker.call("session_status",{}),15)
                    self.assertTrue(status["signed_in"])
                    orders=await asyncio.wait_for(worker.call("list_orders",{}),5)
                    self.assertEqual(orders,[{"order_number":"111111-0001"}])
                finally:await worker.stop()
                with self.assertRaises(ProcessLookupError):os.kill(status["pid"],0)


if __name__=="__main__":unittest.main()
