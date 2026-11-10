"""Tiny storage layer. The same SQLite SQL runs on a local file or on Cloudflare D1."""

import os
import threading
from typing import Any, Protocol


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('agent', 'admin')),
  password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
  id                INTEGER PRIMARY KEY,
  message_id        TEXT UNIQUE,
  from_name         TEXT NOT NULL DEFAULT '',
  from_email        TEXT NOT NULL,
  subject           TEXT NOT NULL,
  body              TEXT NOT NULL,
  source            TEXT NOT NULL CHECK (source IN ('webhook', 'form')),
  status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'replied', 'closed')),
  assignee_id       INTEGER REFERENCES users(id),
  created_at        INTEGER NOT NULL,
  category          TEXT,
  urgency           TEXT,
  draft_reply       TEXT,
  article_id        TEXT,
  triage_error      TEXT,
  triage_attempts   INTEGER NOT NULL DEFAULT 0,
  triage_lock_until INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tickets_status_created ON tickets (status, created_at);
CREATE TABLE IF NOT EXISTS replies (
  id        INTEGER PRIMARY KEY,
  ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  body      TEXT NOT NULL,
  sent_by   INTEGER NOT NULL REFERENCES users(id),
  sent_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS replies_ticket ON replies (ticket_id);
"""


class Database(Protocol):
    async def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]: ...
    async def execute(self, sql: str, params: tuple = ()) -> int: ...  # returns rows changed


class SQLiteDB:
    """Local storage (Docker Compose, tests). SQLite is fast enough to call inline."""

    def __init__(self, path: str):
        import sqlite3  # imported lazily: not needed (or bundled) on Workers

        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.Lock()

    async def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    async def execute(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).rowcount


class D1DB:
    """Cloudflare D1 through the Worker's binding (production). Same SQL as SQLite."""

    def __init__(self, binding):
        self.binding = binding

    async def query(self, sql, params=()):
        res = await self.binding.prepare(sql).bind(*params).all()
        return [dict(r) for r in res.results]

    async def execute(self, sql, params=()):
        res = await self.binding.prepare(sql).bind(*params).run()
        return res.meta["changes"]


async def init_schema(db: Database) -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            await db.execute(stmt)
