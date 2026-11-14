"""SQLite model with strict per-Telegram-user data ownership and one-use invitations."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.prices import should_alert


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Notice:
    item_id: int
    sku: str
    title: str
    url: str
    source_type: str
    source_ref: str
    base_price: int
    current_price: int
    threshold: int
    user_id: int = 1


class Store:
    """A user-scoped store. All item/settings methods implicitly filter user_id."""

    def __init__(self, path: str, owner_id: int = 1, user_id: int | None = None):
        if owner_id <= 0:
            raise ValueError("Owner ID must be positive")
        self.path = path
        self.owner_id = owner_id
        self.user_id = user_id if user_id is not None else owner_id
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as conn:
            # Serialize first-start/migration across bot and login-web containers.
            conn.execute("BEGIN EXCLUSIVE")
            self._schema(conn)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=20)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=20000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _schema(self, conn: sqlite3.Connection) -> None:
        # One-time migration from v0.3's single-owner schema; keep old row IDs
        # because alerts and history refer to them. The old owner becomes admin.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone():
            cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
            if "user_id" not in cols:
                conn.execute("ALTER TABLE items RENAME TO items_single_owner")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            cols = {r[1] for r in conn.execute("PRAGMA table_info(settings)")}
            if "user_id" not in cols:
                conn.execute("ALTER TABLE settings RENAME TO settings_single_owner")
        schema="""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL CHECK(role IN ('owner','member')),
            revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY(user_id,key)
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source_type TEXT NOT NULL, source_ref TEXT NOT NULL, sku TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'active', base_price INTEGER,
            last_price INTEGER, last_alert_price INTEGER, threshold INTEGER,
            active INTEGER NOT NULL DEFAULT 1, muted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id,source_type,source_ref,sku)
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id),
            price INTEGER NOT NULL, observed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id),
            price INTEGER NOT NULL, sent_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invites (
            token_hash TEXT PRIMARY KEY, created_by INTEGER NOT NULL,
            expires_at TEXT NOT NULL, used_by INTEGER, used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS login_tokens (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS web_sessions (
            session_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            csrf TEXT NOT NULL, expires_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT 'start'
        );
        CREATE INDEX IF NOT EXISTS idx_items_status ON items(user_id,active,source_type);
        CREATE INDEX IF NOT EXISTS idx_history_item ON price_history(item_id,observed_at);
        """
        for statement in schema.split(";"):
            if statement.strip():
                conn.execute(statement)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='items_single_owner'").fetchone():
            conn.execute("""INSERT INTO items(id,user_id,source_type,source_ref,sku,title,url,state,
                base_price,last_price,last_alert_price,threshold,active,muted,updated_at)
                SELECT id,?,source_type,source_ref,sku,title,url,state,base_price,last_price,
                       last_alert_price,threshold,active,muted,updated_at FROM items_single_owner""", (self.owner_id,))
            conn.execute("DROP TABLE items_single_owner")
            # SQLite updated the legacy FK targets during RENAME. Rebuild
            # history tables so their REFERENCES point to new items, not a
            # now-deleted table. Retain primary keys and original history.
            for table, time_col in (("price_history", "observed_at"), ("alerts", "sent_at")):
                conn.execute(f"ALTER TABLE {table} RENAME TO {table}_single_owner")
                conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                             f"item_id INTEGER NOT NULL REFERENCES items(id), "
                             f"price INTEGER NOT NULL, {time_col} TEXT NOT NULL)")
                conn.execute(f"INSERT INTO {table}(id,item_id,price,{time_col}) "
                             f"SELECT id,item_id,price,{time_col} FROM {table}_single_owner")
                conn.execute(f"DROP TABLE {table}_single_owner")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(user_id,active,source_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_item ON price_history(item_id,observed_at)")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='settings_single_owner'").fetchone():
            conn.execute("INSERT INTO settings(user_id,key,value) SELECT ?,key,value FROM settings_single_owner", (self.owner_id,))
            conn.execute("DROP TABLE settings_single_owner")
        conn.execute("INSERT INTO users(user_id,role,created_at) VALUES(?,'owner',?) "
                     "ON CONFLICT(user_id) DO UPDATE SET role='owner',revoked=0", (self.owner_id, now()))

    def for_user(self, user_id: int) -> "Store":
        # Construct a lightweight view without re-running schema/migrations.
        scoped = object.__new__(Store)
        scoped.path, scoped.owner_id, scoped.user_id = self.path, self.owner_id, int(user_id)
        return scoped

    def is_allowed(self, user_id: int) -> bool:
        with self.db() as conn:
            return conn.execute("SELECT 1 FROM users WHERE user_id=? AND revoked=0", (user_id,)).fetchone() is not None

    def active_users(self) -> list[sqlite3.Row]:
        with self.db() as conn:
            return conn.execute("SELECT * FROM users WHERE revoked=0 ORDER BY role DESC,created_at").fetchall()

    def make_invite(self, actor: int, ttl: int = 7 * 86400) -> str:
        if actor != self.owner_id or not self.is_allowed(actor):
            raise PermissionError("Only owner can invite")
        token = secrets.token_urlsafe(24)
        with self.db() as conn:
            conn.execute("INSERT INTO invites(token_hash,created_by,expires_at) VALUES(?,?,?)", (digest(token),actor,future(ttl)))
        return token

    def accept_invite(self, token: str, user_id: int, display_name: str) -> bool:
        if not token or user_id <= 0:
            return False
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT 1 FROM invites WHERE token_hash=? AND used_by IS NULL AND expires_at>?", (digest(token),now())).fetchone()
            if not row:
                return False
            # A small family deployment should not accidentally turn public.
            count=conn.execute("SELECT COUNT(*) FROM users WHERE revoked=0").fetchone()[0]
            if count>=12 and not conn.execute("SELECT 1 FROM users WHERE user_id=? AND revoked=0",(user_id,)).fetchone():
                return False
            conn.execute("UPDATE invites SET used_by=?,used_at=? WHERE token_hash=? AND used_by IS NULL",(user_id,now(),digest(token)))
            conn.execute("INSERT INTO users(user_id,display_name,role,revoked,created_at) VALUES(?,?,'member',0,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,revoked=0", (user_id,display_name[:100],now()))
            return True

    def revoke(self, actor: int, user_id: int) -> bool:
        if actor != self.owner_id or user_id == self.owner_id:
            raise PermissionError("Only owner can revoke other users")
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("UPDATE users SET revoked=1 WHERE user_id=? AND revoked=0", (user_id,)).rowcount
            conn.execute("DELETE FROM invites WHERE used_by=?", (user_id,))
            conn.execute("DELETE FROM login_tokens WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM web_sessions WHERE user_id=?", (user_id,))
            return bool(count)

    def issue_login_token(self, user_id: int, ttl: int = 600) -> str:
        if not self.is_allowed(user_id):
            raise PermissionError("Not allowed")
        token = secrets.token_urlsafe(32)
        with self.db() as conn:
            conn.execute("DELETE FROM login_tokens WHERE user_id=?", (user_id,))
            conn.execute("INSERT INTO login_tokens(token_hash,user_id,expires_at) VALUES(?,?,?)", (digest(token),user_id,future(ttl)))
        return token

    def consume_login_token(self, token: str) -> tuple[str,str] | None:
        if len(token) < 32:
            return None
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT t.user_id FROM login_tokens t JOIN users u ON u.user_id=t.user_id "
                               "WHERE t.token_hash=? AND t.expires_at>? AND u.revoked=0", (digest(token),now())).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM login_tokens WHERE token_hash=?", (digest(token),))
            session = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            conn.execute("INSERT INTO web_sessions(session_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)",
                         (digest(session),int(row["user_id"]),csrf,future(1200)))
            return session, csrf

    def web_session(self, session: str) -> sqlite3.Row | None:
        if not session or len(session) < 32:
            return None
        with self.db() as conn:
            return conn.execute("SELECT w.* FROM web_sessions w JOIN users u ON u.user_id=w.user_id "
                                "WHERE w.session_hash=? AND w.expires_at>? AND u.revoked=0",
                                (digest(session),now())).fetchone()

    def web_attempt(self, session: str) -> int:
        with self.db() as conn:
            conn.execute("UPDATE web_sessions SET attempts=attempts+1 WHERE session_hash=?",(digest(session),))
            row=conn.execute("SELECT attempts FROM web_sessions WHERE session_hash=?",(digest(session),)).fetchone()
            return int(row[0]) if row else 999

    def web_stage(self, session: str, stage: str) -> None:
        if stage not in {"start", "code"}:
            raise ValueError("Invalid stage")
        with self.db() as conn:
            conn.execute("UPDATE web_sessions SET stage=? WHERE session_hash=?",(stage,digest(session)))

    def end_web_session(self, session: str) -> None:
        with self.db() as conn:
            conn.execute("DELETE FROM web_sessions WHERE session_hash=?",(digest(session),))

    def erase_user_data(self, user_id: int) -> None:
        with self.db() as conn:
            ids = [int(r[0]) for r in conn.execute("SELECT id FROM items WHERE user_id=?",(user_id,))]
            for item_id in ids:
                conn.execute("DELETE FROM alerts WHERE item_id=?",(item_id,))
                conn.execute("DELETE FROM price_history WHERE item_id=?",(item_id,))
            conn.execute("DELETE FROM items WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM settings WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM login_tokens WHERE user_id=?",(user_id,))
            conn.execute("DELETE FROM web_sessions WHERE user_id=?",(user_id,))

    def get_setting(self, key: str, fallback: str = "") -> str:
        with self.db() as conn:
            row=conn.execute("SELECT value FROM settings WHERE user_id=? AND key=?", (self.user_id,key)).fetchone()
            return str(row["value"]) if row else fallback

    def set_setting(self, key: str, value: str) -> None:
        with self.db() as conn:
            conn.execute("INSERT INTO settings(user_id,key,value) VALUES(?,?,?) "
                         "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",(self.user_id,key,value))

    @property
    def default_threshold(self) -> int:
        return int(self.get_setting("default_threshold","100"))

    def set_default_threshold(self, threshold: int) -> None:
        if not 1 <= threshold <= 1_000_000:
            raise ValueError("Amount must be between 1 and 1,000,000 rubles")
        self.set_setting("default_threshold",str(threshold))

    def observe(self, *, source_type: str, source_ref: str, sku: str, title: str, url: str,
                price: int | None, base_price: int | None = None, state: str = "active") -> Notice | None:
        if not self.is_allowed(self.user_id):
            raise PermissionError("User access has been revoked")
        if source_type not in {"cart","order","manual"} or not sku.isdigit():
            raise ValueError("Invalid source or SKU")
        if price is not None and price <= 0: price = None
        if base_price is not None and base_price <= 0: base_price = None
        at=now()
        with self.db() as conn:
            old=conn.execute("SELECT * FROM items WHERE user_id=? AND source_type=? AND source_ref=? AND sku=?",
                             (self.user_id,source_type,source_ref,sku)).fetchone()
            if old is None:
                baseline = base_price if base_price is not None else price
                cursor=conn.execute("INSERT INTO items(user_id,source_type,source_ref,sku,title,url,state,base_price,last_price,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                    (self.user_id,source_type,source_ref,sku,title,url,state,baseline,price,at))
                item_id=int(cursor.lastrowid)
                previous,muted,custom_threshold,last_alert = None,0,None,None
            else:
                item_id=int(old["id"])
                baseline=old["base_price"]
                last_alert=old["last_alert_price"]
                if source_type=="cart" and not old["active"]:
                    baseline=base_price if base_price is not None else price
                    last_alert=None
                    conn.execute("UPDATE items SET last_alert_price=NULL WHERE id=? AND user_id=?",(item_id,self.user_id))
                elif baseline is None:
                    baseline=base_price if base_price is not None else price
                previous=old["last_price"]
                muted=old["muted"]
                custom_threshold=old["threshold"]
                conn.execute("UPDATE items SET title=?,url=?,state=?,base_price=?,last_price=COALESCE(?,last_price),active=?,updated_at=? WHERE id=? AND user_id=?",
                             (title or old["title"],url or old["url"],state,baseline,price,int(state=="active"),at,item_id,self.user_id))
            if price is not None and price!=previous:
                conn.execute("INSERT INTO price_history(item_id,price,observed_at) VALUES(?,?,?)",(item_id,price,at))
            if state!="active" or muted:
                return None
            threshold=int(custom_threshold) if custom_threshold is not None else self.default_threshold
            if not should_alert(baseline,price,last_alert,threshold):
                return None
            return Notice(item_id=item_id,sku=sku,title=title or sku,url=url or f"https://www.ozon.ru/product/{sku}/",
                          source_type=source_type,source_ref=source_ref,base_price=int(baseline),
                          current_price=int(price),threshold=threshold,user_id=self.user_id)

    def mark_alert_sent(self, notice: Notice) -> None:
        if notice.user_id != self.user_id:
            raise PermissionError("Alert belongs to a different user")
        with self.db() as conn:
            cursor=conn.execute("UPDATE items SET last_alert_price=? WHERE id=? AND user_id=? AND last_price=?",
                                (notice.current_price,notice.item_id,self.user_id,notice.current_price))
            if cursor.rowcount:
                conn.execute("INSERT INTO alerts(item_id,price,sent_at) VALUES(?,?,?)",(notice.item_id,notice.current_price,now()))

    def list_items(self, source_type: str | None = None, active_only: bool = False) -> list[sqlite3.Row]:
        sql="SELECT * FROM items WHERE user_id=?"
        params:list[str|int]=[self.user_id]
        if source_type:
            sql+=" AND source_type=?";params.append(source_type)
        if active_only: sql+=" AND active=1"
        sql+=" ORDER BY active DESC,updated_at DESC,id DESC"
        with self.db() as conn:
            return conn.execute(sql,params).fetchall()

    def has_item(self, source_type: str, source_ref: str, sku: str) -> bool:
        with self.db() as conn:
            return conn.execute("SELECT 1 FROM items WHERE user_id=? AND source_type=? AND source_ref=? AND sku=?",
                                (self.user_id,source_type,source_ref,sku)).fetchone() is not None

    def retire_missing_cart(self, present_skus: set[str]) -> int:
        with self.db() as conn:
            rows=conn.execute("SELECT id,sku FROM items WHERE user_id=? AND source_type='cart' AND active=1", (self.user_id,)).fetchall()
            missing=[int(r["id"]) for r in rows if r["sku"] not in present_skus]
            conn.executemany("UPDATE items SET active=0,state='removed',updated_at=? WHERE id=? AND user_id=?",
                             [(now(),i,self.user_id) for i in missing])
            return len(missing)

    def close_order(self, order_ref: str, sku: str, state: str) -> None:
        if state not in {"received","cancelled"}:return
        with self.db() as conn:
            conn.execute("UPDATE items SET active=0,state=?,updated_at=? WHERE user_id=? AND source_type='order' AND source_ref=? AND sku=?",
                         (state,now(),self.user_id,order_ref,sku))

    def set_threshold(self, identifier: str, threshold: int | None) -> int:
        if threshold is not None and not 1 <= threshold <= 1_000_000:
            raise ValueError("Amount must be between 1 and 1,000,000 rubles")
        with self.db() as conn:
            if identifier.startswith("#") and identifier[1:].isdigit():
                cursor=conn.execute("UPDATE items SET threshold=? WHERE id=? AND user_id=?",(threshold,int(identifier[1:]),self.user_id))
            else:
                cursor=conn.execute("UPDATE items SET threshold=? WHERE sku=? AND user_id=?",(threshold,identifier,self.user_id))
            return cursor.rowcount

    def set_muted(self, identifier: str, muted: bool) -> int:
        with self.db() as conn:
            if identifier.startswith("#") and identifier[1:].isdigit():
                cursor=conn.execute("UPDATE items SET muted=? WHERE id=? AND user_id=?",(int(muted),int(identifier[1:]),self.user_id))
            else:
                cursor=conn.execute("UPDATE items SET muted=? WHERE sku=? AND user_id=?",(int(muted),identifier,self.user_id))
            return cursor.rowcount

    def forget_manual(self, identifier: str) -> int:
        with self.db() as conn:
            if identifier.startswith("#") and identifier[1:].isdigit():
                rows=conn.execute("SELECT id FROM items WHERE user_id=? AND id=? AND source_type='manual'",(self.user_id,int(identifier[1:]))).fetchall()
            else:
                rows=conn.execute("SELECT id FROM items WHERE user_id=? AND sku=? AND source_type='manual'",(self.user_id,identifier)).fetchall()
            for record in rows:
                conn.execute("DELETE FROM alerts WHERE item_id=?",(record[0],))
                conn.execute("DELETE FROM price_history WHERE item_id=?",(record[0],))
                conn.execute("DELETE FROM items WHERE id=? AND user_id=?",(record[0],self.user_id))
            return len(rows)

    def seen_order_references(self) -> set[str]:
        with self.db() as conn:
            return {str(r[0]).split("|",1)[0] for r in conn.execute(
                "SELECT DISTINCT source_ref FROM items WHERE user_id=? AND source_type='order' AND active=1",(self.user_id,)).fetchall()}
