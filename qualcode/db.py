"""SQLite storage. One file, no server, easy to inspect or back up."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS codes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    definition TEXT NOT NULL DEFAULT '',
    include_when TEXT NOT NULL DEFAULT '',
    exclude_when TEXT NOT NULL DEFAULT ''
);
-- Examples are the "memory" the model learns from. They come from three places:
--   user       : typed into the codebook editor
--   accepted   : a passage the researcher accepted for a code
--   correction : a rejection or re-code made in the review screen
-- polarity 'negative' means "this passage is NOT an instance of the code".
CREATE TABLE IF NOT EXISTS examples (
    id INTEGER PRIMARY KEY,
    code_id INTEGER NOT NULL REFERENCES codes(id) ON DELETE CASCADE,
    quote TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    polarity TEXT NOT NULL DEFAULT 'positive' CHECK (polarity IN ('positive','negative')),
    source TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user','accepted','correction')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL
);
-- Every model call is logged with the exact prompt sent and the raw reply.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    user_prompt TEXT NOT NULL,
    context_refs TEXT NOT NULL,      -- JSON list of ref ids the model was shown
    raw_response TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
    code_id INTEGER NOT NULL REFERENCES codes(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    origin TEXT NOT NULL DEFAULT 'model' CHECK (origin IN ('model','human')),
    evidence_quote TEXT NOT NULL DEFAULT '',
    justification TEXT NOT NULL DEFAULT '',
    cited_refs TEXT NOT NULL DEFAULT '[]',   -- JSON list
    confidence TEXT NOT NULL DEFAULT 'medium',
    flags TEXT NOT NULL DEFAULT '[]',        -- JSON list of validation warnings
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
    reviewer_note TEXT NOT NULL DEFAULT '',
    learned_example_id INTEGER REFERENCES examples(id) ON DELETE SET NULL,  -- example created by review
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

JSON_COLUMNS = {"cited_refs", "flags", "context_refs"}


class DB:
    """Thin wrapper around sqlite3. Thread-safe enough for a single-user local app."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_COLUMNS & d.keys():
            d[k] = json.loads(d[k])
        return d

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [self._row(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._row(self.conn.execute(sql, params).fetchone())

    def run(self, sql: str, params: tuple = ()) -> int:
        """Execute a write and return lastrowid."""
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def insert(self, table: str, **values: Any) -> int:
        for k, v in list(values.items()):
            if k in JSON_COLUMNS and not isinstance(v, str):
                values[k] = json.dumps(v)
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return self.run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))

    def update(self, table: str, row_id: int, **values: Any) -> None:
        if not values:
            return
        for k, v in list(values.items()):
            if k in JSON_COLUMNS and not isinstance(v, str):
                values[k] = json.dumps(v)
        sets = ", ".join(f"{k} = ?" for k in values)
        self.run(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.run(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
