"""Multi-tenant data boundary, one-use invites, web-login tokens and legacy migration."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage import Store
from app.gateway_client import GatewayError


class FamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/"watcher.db")
        self.root=Store(self.path,owner_id=101)
        self.bob=self.root.for_user(202)
        self.alice=self.root.for_user(303)

    def invite(self,user_id:int):
        token=self.root.make_invite(101)
        self.assertTrue(self.root.accept_invite(token,user_id,"Family"))
        return token

    def test_owner_only_invite_and_single_use(self):
        with self.assertRaises(PermissionError):self.root.make_invite(202)
        token=self.invite(202)
        self.assertFalse(self.root.accept_invite(token,303,"Intruder"))
        self.assertTrue(self.root.is_allowed(202))
        self.assertFalse(self.root.is_allowed(303))

    def test_expired_invite(self):
        token=self.root.make_invite(101,ttl=-5)
        self.assertFalse(self.root.accept_invite(token,202,"Late"))

    def test_private_prices_thresholds_items_and_order_state(self):
        self.invite(202)
        self.invite(303)
        for st,base in [(self.bob,1300),(self.alice,3400)]:
            st.observe(source_type="cart",source_ref="cart",sku="12345678",title="Item",url="",price=base)
            st.observe(source_type="order",source_ref="7777-1234",sku="12345678",title="Item",url="",base_price=base,price=base)
        self.bob.set_default_threshold(500)
        self.assertEqual(self.alice.default_threshold,100)
        self.assertEqual(len(self.bob.list_items()),2)
        self.assertEqual(len(self.alice.list_items()),2)
        self.assertEqual(self.alice.list_items("cart")[0]["base_price"],3400)
        self.assertEqual(self.bob.list_items("cart")[0]["base_price"],1300)
        self.assertEqual(self.bob.set_threshold("12345678",250),2)
        self.assertEqual(self.alice.list_items("cart")[0]["threshold"],None)
        item_id=self.bob.list_items("cart")[0]["id"]
        self.assertEqual(self.alice.set_threshold(f"#{item_id}",5),0)
        self.assertEqual(self.alice.set_muted(f"#{item_id}",True),0)
        self.assertEqual(self.alice.forget_manual(f"#{item_id}"),0)
        self.bob.close_order("7777-1234","12345678","received")
        self.assertEqual(self.alice.list_items("order")[0]["state"],"active")
        self.assertEqual(self.alice.retire_missing_cart(set()),1)
        self.assertTrue(self.bob.list_items("cart")[0]["active"])
        notice=self.bob.observe(source_type="cart",source_ref="cart",sku="12345678",title="Item",url="",price=1000)
        self.assertIsNotNone(notice)
        self.assertEqual(notice.user_id,202)
        with self.assertRaises(PermissionError):self.alice.mark_alert_sent(notice)

    def test_revoke_prevents_login_and_own_access(self):
        self.invite(202)
        token=self.root.issue_login_token(202)
        self.assertTrue(self.root.revoke(101,202))
        self.assertFalse(self.root.is_allowed(202))
        self.assertIsNone(self.root.consume_login_token(token))
        with self.assertRaises(PermissionError):self.root.revoke(202,303)
        with self.assertRaises(PermissionError):self.root.revoke(101,101)

    def test_login_link_consumed_only_once_and_session_scoped(self):
        self.invite(202)
        self.invite(303)
        token=self.root.issue_login_token(202)
        sid,csrf=self.root.consume_login_token(token)
        self.assertIsNone(self.root.consume_login_token(token))
        row=self.root.web_session(sid)
        self.assertEqual(row["user_id"],202)
        self.assertEqual(row["csrf"],csrf)
        self.assertEqual(row["stage"],"start")
        self.root.web_stage(sid,"code")
        self.assertEqual(self.root.web_attempt(sid),1)
        self.assertEqual(self.root.web_session(sid)["stage"],"code")
        self.root.end_web_session(sid)
        self.assertIsNone(self.root.web_session(sid))

    def test_revoke_erases_only_target_member_records(self):
        self.invite(202)
        self.invite(303)
        for uid in (202,303):
            s=self.root.for_user(uid)
            s.observe(source_type="manual",source_ref="manual",sku="12345678",title="X",url="",price=300)
            s.set_default_threshold(170)
        self.root.revoke(101,202)
        self.root.erase_user_data(202)
        self.assertEqual(self.root.for_user(202).list_items(),[])
        self.assertEqual(len(self.root.for_user(303).list_items()),1)
        self.assertEqual(self.root.for_user(303).default_threshold,170)

    def test_legacy_one_owner_data_kept(self):
        old=str(Path(self.tmp.name)/"old.db")
        with closing(sqlite3.connect(old)) as conn, conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY,source_type TEXT NOT NULL, source_ref TEXT NOT NULL, sku TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'active', base_price INTEGER,last_price INTEGER,last_alert_price INTEGER,threshold INTEGER,active INTEGER NOT NULL DEFAULT 1, muted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, UNIQUE(source_type,source_ref,sku))")
            conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("CREATE TABLE price_history(id INTEGER PRIMARY KEY AUTOINCREMENT,item_id INTEGER NOT NULL REFERENCES items(id), price INTEGER NOT NULL, observed_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,item_id INTEGER NOT NULL REFERENCES items(id), price INTEGER NOT NULL, sent_at TEXT NOT NULL)")
            conn.execute("INSERT INTO items(id,source_type,source_ref,sku,title,base_price,last_price,updated_at) VALUES(10,'manual','manual','12345678','Old product',500,500,'2026-01-01')")
            conn.execute("INSERT INTO settings(key,value) VALUES('default_threshold','200')")
            conn.execute("INSERT INTO price_history(item_id,price,observed_at) VALUES(10,500,'2026-01-01')")
            conn.execute("INSERT INTO alerts(item_id,price,sent_at) VALUES(10,500,'2026-01-01')")
        restored=Store(old,owner_id=101)
        self.assertEqual(restored.list_items()[0]["id"],10)
        self.assertEqual(restored.list_items()[0]["user_id"],101)
        self.assertEqual(restored.default_threshold,200)
        with closing(sqlite3.connect(old)) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_list(price_history)").fetchone()[2],"items")
            self.assertEqual(conn.execute("PRAGMA foreign_key_list(alerts)").fetchone()[2],"items")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],1)
        self.assertEqual(restored.for_user(303).list_items(),[])
        # Migration must be safe across subsequent restarts.
        restored2=Store(old,owner_id=101)
        self.assertEqual(len(restored2.list_items()),1)


if __name__=="__main__":unittest.main()
