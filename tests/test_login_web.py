"""Web login link never exposes another person's account or accepts forged POST."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from app.login_web import COOKIE, build_app
from app.storage import Store


class LoginWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(str(Path(self.tmp.name)/"db.sqlite"),owner_id=111)
        invitation=self.store.make_invite(111)
        self.assertTrue(self.store.accept_invite(invitation,222,"Relative"))
        self.app=build_app(self.store,"http://ozon-gateway:8085","a"*48)
        self.client=TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_single_use_exchange_and_clear_token_url(self):
        token=self.store.issue_login_token(222)
        response=await self.client.get("/login?t="+token,allow_redirects=False)
        self.assertEqual(response.status,303)
        self.assertEqual(response.headers["Location"],"/login")
        self.assertEqual(response.cookies[COOKIE]["secure"],True)
        self.assertEqual(response.cookies[COOKIE]["httponly"],True)
        # Opening /connect from Telegram is cross-site. Strict drops this
        # cookie on the redirect, causing "Нужна новая ссылка" every time.
        self.assertEqual(response.cookies[COOKIE]["samesite"],"Lax")
        again=await self.client.get("/login?t="+token,allow_redirects=False)
        self.assertEqual(again.status,200)
        self.assertIn("Ссылка недействительна",await again.text())
        self.assertNotIn(token,await again.text())

    async def test_not_logged_in_without_cookie(self):
        response=await self.client.get("/login")
        self.assertIn("Нужна новая ссылка",await response.text())
        self.assertEqual(response.headers["Referrer-Policy"],"no-referrer")

    async def test_two_stage_login_keeps_user_scope_and_does_not_store_code(self):
        calls=[]
        class StubGateway:
            async def call(self, tool, **args):
                calls.append((tool,args))
                if tool == "session_status": return {"signed_in":False}
                if tool == "start_login": return {"stage":"code_requested"}
                if tool == "submit_login_code": return {"signed_in":True}
                raise AssertionError(tool)

        @asynccontextmanager
        async def fake_gateway(url, token, user_id):
            self.assertEqual(user_id,222)
            yield StubGateway()

        invitation=self.store.issue_login_token(222)
        sid,csrf=self.store.consume_login_token(invitation)
        headers={"Cookie":f"{COOKIE}={sid}"}
        with patch("app.login_web.connect_gateway",fake_gateway):
            resp=await self.client.post("/login/start",data={"csrf":csrf,"login":"+79001234567"},headers=headers,allow_redirects=False)
            self.assertEqual(resp.status,303)
            self.assertEqual(self.store.web_session(sid)["stage"],"code")
            resp=await self.client.post("/login/confirm",data={"csrf":csrf,"code":"123456"},headers=headers)
            self.assertEqual(resp.status,200)
            self.assertIn("Ozon подключён",await resp.text())
            self.assertIsNone(self.store.web_session(sid))
            self.assertEqual([x[0] for x in calls],["session_status","start_login","submit_login_code"])
            with self.store.db() as conn:
                # No OTP in SQLite, URLs or tokens.
                self.assertNotIn("123456",str(conn.execute("SELECT * FROM web_sessions").fetchall()))

    async def test_csrf_and_revocation_reject_login_post(self):
        token=self.store.issue_login_token(222)
        sid,csrf=self.store.consume_login_token(token)
        headers={"Cookie":f"{COOKIE}={sid}"}
        response=await self.client.post("/login/start",data={"csrf":"wrong","login":"79001234567"},headers=headers)
        self.assertEqual(response.status,403)
        self.store.revoke(111,222)
        response=await self.client.post("/login/start",data={"csrf":csrf,"login":"79001234567"},headers=headers)
        self.assertEqual(response.status,403)


if __name__=="__main__":unittest.main()
