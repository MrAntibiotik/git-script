"""Локальний стан (SQLite): черга, слоти, журнал подій. Переживає перезапуск ПК."""
from __future__ import annotations

import json
import sqlite3
import threading
from config import now_ts
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mp4           TEXT NOT NULL UNIQUE,
    json          TEXT NOT NULL,
    game          TEXT DEFAULT '',
    added_at      INTEGER NOT NULL,
    qpos          INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    skips         INTEGER NOT NULL DEFAULT 0,
    slot_at       INTEGER,
    tg_msg        INTEGER,
    tg_msg_is_text INTEGER NOT NULL DEFAULT 0,
    tg_text_msg   INTEGER,
    disabled      TEXT NOT NULL DEFAULT '[]',
    title_override TEXT,
    edited        TEXT NOT NULL DEFAULT '[]',
    results       TEXT NOT NULL DEFAULT '{}',
    posted_at     INTEGER,
    stats_sent    INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS items_status ON items(status, qpos);

CREATE TABLE IF NOT EXISTS slots (
    slot_at  INTEGER PRIMARY KEY,
    source   TEXT NOT NULL DEFAULT 'config',
    item_id  INTEGER,
    state    TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    item_id  INTEGER,
    kind     TEXT NOT NULL,
    details  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

JSON_COLS = {"disabled": list, "edited": list, "results": dict}
# Статуси, у яких шортс ще «живий» у робочому процесі
ACTIVE = ("queued", "notified", "approved", "publishing", "failed")


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)

    # ---- базові операції ----
    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # ---- items ----
    @staticmethod
    def _item(row: dict | None) -> dict | None:
        if row is None:
            return None
        for col, typ in JSON_COLS.items():
            try:
                row[col] = json.loads(row[col] or "null") or typ()
            except (TypeError, ValueError):
                row[col] = typ()
        return row

    def get_item(self, item_id: int) -> dict | None:
        return self._item(self.one("SELECT * FROM items WHERE id=?", (item_id,)))

    def items(self, where: str = "1", args=(), order: str = "qpos") -> list[dict]:
        return [self._item(r) for r in self.q(f"SELECT * FROM items WHERE {where} ORDER BY {order}", args)]

    def update_item(self, item_id: int, **fields) -> None:
        if not fields:
            return
        sets, vals = [], []
        for k, v in fields.items():
            if k in JSON_COLS:
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(item_id)
        self.x(f"UPDATE items SET {', '.join(sets)} WHERE id=?", vals)

    def add_item(self, mp4: str, json_path: str, game: str) -> int | None:
        with self.tx():
            if self.conn.execute("SELECT 1 FROM items WHERE mp4=?", (mp4,)).fetchone():
                return None
            qpos = self.next_qpos()
            cur = self.conn.execute(
                "INSERT INTO items(mp4, json, game, added_at, qpos) VALUES (?,?,?,?,?)",
                (mp4, json_path, game, now_ts(), qpos),
            )
            return cur.lastrowid

    def next_qpos(self) -> int:
        r = self.one("SELECT COALESCE(MAX(qpos), 0) AS m FROM items")
        return int(r["m"]) + 1

    def first_qpos(self) -> int:
        r = self.one("SELECT COALESCE(MIN(qpos), 1) AS m FROM items WHERE status='queued'")
        return int(r["m"]) - 1

    def queue(self) -> list[dict]:
        return self.items("status='queued'")

    def known_paths(self) -> set[str]:
        return {r["mp4"] for r in self.q("SELECT mp4 FROM items")}

    # ---- slots ----
    def slot(self, slot_at: int) -> dict | None:
        return self.one("SELECT * FROM slots WHERE slot_at=?", (slot_at,))

    def set_slot(self, slot_at: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE slots SET {sets} WHERE slot_at=?", (*fields.values(), slot_at))

    # ---- events / kv ----
    def event(self, item_id: int | None, kind: str, details=None) -> None:
        if details is not None and not isinstance(details, str):
            details = json.dumps(details, ensure_ascii=False)
        self.x("INSERT INTO events(ts, item_id, kind, details) VALUES (?,?,?,?)",
               (now_ts(), item_id, kind, details))

    def events(self, start: int, end: int) -> list[dict]:
        rows = self.q("SELECT * FROM events WHERE ts>=? AND ts<? ORDER BY ts", (start, end))
        for r in rows:
            try:
                r["details"] = json.loads(r["details"]) if r["details"] else None
            except ValueError:
                pass
        return rows

    def get(self, key: str, default=None):
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def set(self, key: str, value) -> None:
        self.x("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, None if value is None else str(value)))
