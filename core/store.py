"""
Storage for the watchlist, the alert feed and small job state.

Two backends with the same interface (STORE_BACKEND):
- local     LocalStore: a SQLite file in .cache/, so the app, CLI and tests run on a laptop.
- dynamodb  DynamoStore (core/store_dynamo.py, Phase B): the single-table design in SPEC §4.

Dedupe is done by the database: events.dedupe_key is UNIQUE, and insert_event() returns the new
row only if the key was new. That one rule guarantees "one alert per rule per day".

Ported from v1 core/store.py (73b6fda): SupabaseStore and the pgvector chunk methods are cut
(D4); get_store() validates STORE_BACKEND through core.config.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from core import config
from core.config import env

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = [
    {"ticker": "NVDA", "name": "NVIDIA", "cik": 1045810},
    {"ticker": "AMD", "name": "AMD", "cik": 2488},
    {"ticker": "INTC", "name": "Intel", "cik": 50863},
]
DEFAULT_PCT_MOVE = 3.0
WATCH_FIELDS = ("ticker", "name", "cik", "pct_move", "price_above", "price_below")


class StoreError(RuntimeError):
    """The database could not be reached or rejected a request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SQLITE_SCHEMA = """
create table if not exists watchlist (
  ticker text primary key, name text, cik integer,
  pct_move real not null default 3, price_above real, price_below real,
  added_at text not null);
create table if not exists events (
  id integer primary key autoincrement, ticker text not null, kind text not null,
  title text not null, body text, url text, dedupe_key text not null unique,
  notified integer not null default 0, created_at text not null);
create table if not exists app_state (key text primary key, value text not null, updated_at text);
"""


class LocalStore:
    name = "Local SQLite"

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock, self.db:
            self.db.executescript(_SQLITE_SCHEMA)
            if not self.db.execute("select count(*) from app_state where key='seeded'").fetchone()[0]:
                for w in DEFAULT_WATCHLIST:
                    self.db.execute("insert or ignore into watchlist (ticker, name, cik, added_at) "
                                    "values (?, ?, ?, ?)", (w["ticker"], w["name"], w["cik"], _now()))
                self.db.execute("insert into app_state values ('seeded', 'true', ?)", (_now(),))

    def _rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock, self.db:
            return self.db.execute(sql, args)

    def list_watchlist(self) -> list[dict]:
        return self._rows("select * from watchlist order by added_at, rowid")

    def add_ticker(self, ticker, name=None, cik=None, pct_move=DEFAULT_PCT_MOVE,
                   price_above=None, price_below=None) -> None:
        self._exec(
            "insert into watchlist (ticker, name, cik, pct_move, price_above, price_below, added_at) "
            "values (?, ?, ?, ?, ?, ?, ?) on conflict(ticker) do update set name=excluded.name, "
            "cik=excluded.cik, pct_move=excluded.pct_move, price_above=excluded.price_above, "
            "price_below=excluded.price_below",
            (ticker, name or ticker, cik, pct_move, price_above, price_below, _now()))

    def remove_ticker(self, ticker: str) -> None:
        self._exec("delete from watchlist where ticker = ?", (ticker,))

    def insert_event(self, event: dict) -> dict | None:
        cur = self._exec(
            "insert or ignore into events (ticker, kind, title, body, url, dedupe_key, notified, created_at) "
            "values (?, ?, ?, ?, ?, ?, 0, ?)",
            (event["ticker"], event["kind"], event["title"], event.get("body"), event.get("url"),
             event["dedupe_key"], _now()))
        if cur.rowcount == 0:
            return None
        return self._rows("select * from events where id = ?", (cur.lastrowid,))[0]

    def has_event(self, dedupe_key: str) -> bool:
        return bool(self._rows("select 1 from events where dedupe_key = ? limit 1", (dedupe_key,)))

    def recent_events(self, limit: int = 20) -> list[dict]:
        return self._rows("select * from events order by created_at desc, id desc limit ?", (limit,))

    def unnotified_events(self, since_iso: str) -> list[dict]:
        return self._rows("select * from events where notified = 0 and created_at >= ? order by id",
                          (since_iso,))

    def mark_notified(self, event_id) -> None:
        self._exec("update events set notified = 1 where id = ?", (event_id,))

    def update_event_body(self, event_id, body: str) -> None:
        self._exec("update events set body = ? where id = ?", (body, event_id))

    def get_state(self, key: str, default=None):
        rows = self._rows("select value from app_state where key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value) -> None:
        self._exec("insert into app_state values (?, ?, ?) on conflict(key) do update set "
                   "value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(value), _now()))


# ════════════════════════════════════════════════════════════════════════════

_store = None
_store_lock = threading.Lock()


def get_store():
    """The configured store (STORE_BACKEND = local | dynamodb), created once per process."""
    global _store
    with _store_lock:
        if _store is None:
            if config.store_backend() == "dynamodb":
                from core.store_dynamo import DynamoStore  # boto3 only needed on this path
                _store = DynamoStore(env("DDB_TABLE", "ResearchDesk"), env("AWS_REGION") or None)
            else:
                _store = LocalStore(Path(env("LOCAL_DB_PATH", str(config.CACHE_DIR / "local.db"))))
            logger.info("Store: %s", _store.name)
        return _store


def reset_store() -> None:
    global _store
    with _store_lock:
        _store = None
