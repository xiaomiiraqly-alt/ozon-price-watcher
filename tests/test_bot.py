"""Actual aiogram dispatcher: authorization and destructive commands, no Telegram calls."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import re

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendMessage
from aiogram.types import Chat, Message, Update, User

from app.bot import setup_handlers
from app.config import Config
from app.gateway_client import GatewayError
from app.storage import Store


class TelegramFixture(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent=[]
    async def close(self):pass
    async def make_request(self,bot,method,timeout=None):  # noqa: ASYNC109 - aiogram interface
        if isinstance(method,GetMe):return User(id=123456,is_bot=True,first_name="Watcher",username="test_watcher_bot")
        if isinstance(method,SendMessage):
            self.sent.append(method)
            return Message(message_id=len(self.sent),date=datetime.now(timezone.utc),chat=Chat(id=method.chat_id,type="private"),text=method.text)
        raise AssertionError(type(method).__name__)
    async def stream_content(self,*args,**kwargs):
        yield b""


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(str(Path(self.tmp.name)/"watcher.db"),owner_id=111)
        self.store.accept_invite(self.store.make_invite(111),222,"Family")
        self.session=TelegramFixture()
        self.bot=Bot("123456:TEST_TOKEN",session=self.session)
        self.dp=Dispatcher()
        cfg=Config("123456:TEST_TOKEN","socks5://awg-client:1080",111,"internal","a"*48,"https://example.org",self.store.path,1800,"bank")
        self.monitors=setup_handlers(self.dp,self.store,cfg,self.bot)
        self.dropped=[]
        self.fail_drop=False
        owner=self
        class API:
            def __init__(self,uid):self.uid=uid
            async def drop_account(self):
                if owner.fail_drop:raise GatewayError("offline")
                owner.dropped.append(self.uid)
            async def call(self,*args,**kwargs):return {"signed_in":True}
        @asynccontextmanager
        async def connect(url,token,uid):yield API(uid)
        for name in ("app.bot.connect_gateway","app.monitor.connect_gateway"):
            patcher=patch(name,connect)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.counter=0

    async def asyncTearDown(self):await self.bot.session.close()

    async def send(self,uid,text,chat_type="private"):
        self.counter+=1
        message=Message(message_id=self.counter,date=datetime.now(timezone.utc),chat=Chat(id=uid,type=chat_type),
                        from_user=User(id=uid,is_bot=False,first_name="Family"),text=text)
        await self.dp.feed_update(self.bot,Update(update_id=self.counter,message=message))

    def item(self,uid):
        self.store.for_user(uid).observe(source_type="manual",source_ref="manual",sku="12345678",title="Test",url="",price=1000)

    async def test_uninvited_and_group_commands_do_not_issue_access(self):
        await self.send(333,"/connect")
        await self.send(111,"/invite",chat_type="group")
        await self.send(222,"/invite")
        self.assertEqual(self.session.sent,[])
        with self.store.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM login_tokens").fetchone()[0],0)

    async def test_connect_link_is_scoped_to_sender(self):
        for uid in (111,222):
            await self.send(uid,"/connect")
            link=re.search(r"https://\S+",self.session.sent[-1].text).group(0)
            token=parse_qs(urlsplit(link).query)["t"][0]
            sid,_=self.store.consume_login_token(token)
            self.assertEqual(self.store.web_session(sid)["user_id"],uid)

    async def test_disconnect_erases_only_sender_and_stops_old_monitor(self):
        self.item(111)
        self.item(222)
        await self.send(222,"/connect")
        old=self.monitors[222]
        await self.send(222,"/disconnect confirm")
        self.assertEqual(self.dropped,[222])
        self.assertEqual(self.store.for_user(222).list_items(),[])
        self.assertEqual(len(self.store.list_items()),1)
        self.assertTrue(old._stopped)
        self.assertNotIn(222,self.monitors)

    async def test_failed_disconnect_keeps_data_and_monitor(self):
        self.item(222)
        self.fail_drop=True
        await self.send(222,"/disconnect confirm")
        self.assertEqual(len(self.store.for_user(222).list_items()),1)
        self.assertFalse(self.monitors[222]._stopped)
        self.assertIn("данные не менялись",self.session.sent[-1].text)

    async def test_revoke_is_owner_only_and_cleans_member(self):
        self.item(222)
        await self.send(222,"/revoke 111")
        self.assertTrue(self.store.is_allowed(111))
        await self.send(222,"/connect")
        await self.send(111,"/revoke 222")
        self.assertFalse(self.store.is_allowed(222))
        self.assertEqual(self.store.for_user(222).list_items(),[])
        self.assertEqual(self.dropped,[222])
        self.assertNotIn(222,self.monitors)

    async def test_invalid_page_number_does_not_crash_handler(self):
        await self.send(111,"/items ²")
        self.assertIn("Формат",self.session.sent[-1].text)


if __name__=="__main__":unittest.main()
