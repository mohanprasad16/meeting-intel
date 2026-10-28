"""SQLite storage: one file holds meetings, turns, chunks (+ vectors), the keyword
index, extracted facts and traces.

ponytail: vectors are float32 blobs scanned in numpy; fine to ~100k chunks,
move to Postgres + pgvector beyond that.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS meetings (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER NOT NULL REFERENCES projects(id),
    title             TEXT NOT NULL,
    meeting_date      TEXT,               -- ISO date, NULL when the file has none
    source_format     TEXT NOT NULL,      -- txt | vtt | stencil
    content_hash      TEXT NOT NULL UNIQUE,
    raw_text          TEXT NOT NULL,
    summary           TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | failed
    warnings          TEXT NOT NULL DEFAULT '[]',       -- JSON list
    uploaded_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    line_no     INTEGER NOT NULL,
    time        INTEGER,
    end_time    INTEGER,
    speaker     TEXT NOT NULL,
    text        TEXT NOT NULL,
    time_approx INTEGER NOT NULL DEFAULT 0,
    flagged     INTEGER NOT NULL DEFAULT 0,  -- looks like a prompt injection; never evidence
    UNIQUE (meeting_id, line_no)
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    start_time INTEGER,
    end_time   INTEGER,
    speakers   TEXT NOT NULL,   -- comma separated
    text       TEXT NOT NULL,
    embedding  BLOB            -- float32 vector; NULL if embedding failed
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    line_no    INTEGER NOT NULL,
    quote      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_items (
    id         INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    owner      TEXT,            -- NULL = unassigned
    owner_in_meeting INTEGER NOT NULL DEFAULT 1,
    task       TEXT NOT NULL,
    due_text   TEXT,
    due_date   TEXT,
    status     TEXT NOT NULL DEFAULT 'open',  -- open | updated | done | cancelled
    line_no    INTEGER NOT NULL,
    quote      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_item_updates (
    id             INTEGER PRIMARY KEY,
    action_item_id INTEGER NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    meeting_id     INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    new_status     TEXT NOT NULL,
    note           TEXT,
    line_no        INTEGER NOT NULL,
    quote          TEXT NOT NULL,
    UNIQUE (action_item_id, meeting_id)
);

CREATE TABLE IF NOT EXISTS traces (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,   -- ask | upload
    payload    TEXT NOT NULL,   -- JSON: question, route, ids, guard, timings, tokens
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def to_blob(vector: list[float] | None) -> bytes | None:
    return None if vector is None else np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
