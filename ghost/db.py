"""Connexion et schéma SQLite (~/.ghost/ghost.db).

Règles de séparation : les tables brutes (sessions, events, files_touched,
agents, ingest_log) peuvent être reconstruites par ré-ingestion ; les tables
dérivées des lots 2/3 (candidates, skills) ne sont jamais touchées par un
re-ingest, et le `status` de candidates (triage humain) survit au re-scan.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

DEFAULT_DB = Path.home() / ".ghost" / "ghost.db"

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    project        TEXT NOT NULL,
    cwd            TEXT,
    started_at     TEXT,
    ended_at       TEXT,
    git_branch     TEXT,
    title          TEXT,
    claude_version TEXT,
    n_events       INTEGER NOT NULL DEFAULT 0,
    source         TEXT NOT NULL DEFAULT 'claude-code'
);

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY,
    session_id        TEXT NOT NULL,
    agent_id          TEXT,
    seq               INTEGER NOT NULL,
    ts                TEXT,
    role              TEXT NOT NULL,
    block_type        TEXT NOT NULL,
    tool_name         TEXT,
    tool_use_id       TEXT,
    is_error          INTEGER NOT NULL DEFAULT 0,
    is_human          INTEGER NOT NULL DEFAULT 0,
    text              TEXT,
    payload_json      TEXT,
    payload_truncated INTEGER NOT NULL DEFAULT 0,
    src_file          TEXT NOT NULL,
    src_line          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id    TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    agent_type  TEXT,
    description TEXT,
    tool_use_id TEXT
);

CREATE TABLE IF NOT EXISTS files_touched (
    event_id INTEGER NOT NULL,
    path     TEXT NOT NULL,
    op       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log (
    path        TEXT PRIMARY KEY,
    mtime       REAL NOT NULL,
    sha         TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL,
    signature        TEXT NOT NULL,
    score            REAL NOT NULL DEFAULT 0,
    n_occ            INTEGER NOT NULL DEFAULT 0,
    n_sessions       INTEGER NOT NULL DEFAULT 0,
    session_ids_json TEXT NOT NULL DEFAULT '[]',
    evidence_json    TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'new',
    created_at       TEXT,
    last_seen_at     TEXT,
    UNIQUE(kind, signature)
);

CREATE TABLE IF NOT EXISTS skills (
    id             INTEGER PRIMARY KEY,
    candidate_id   INTEGER NOT NULL,
    slug           TEXT,
    path           TEXT,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    verdict        TEXT NOT NULL,
    low_value      INTEGER NOT NULL DEFAULT 0,
    skip_reason    TEXT,
    critique_line  TEXT,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    created_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_tool_name   ON events(tool_name);
CREATE INDEX IF NOT EXISTS idx_events_tool_use_id ON events(tool_use_id);
CREATE INDEX IF NOT EXISTS idx_events_src_file    ON events(src_file);
CREATE INDEX IF NOT EXISTS idx_files_touched_path ON files_touched(path);
"""


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    """Ouvre la base (créée si absente), applique pragmas et schéma.

    La base contient l'intégralité de l'historique : répertoire en 700,
    fichier en 600.
    """
    db_dir = db_path.parent
    db_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(db_dir, 0o700)
    created = not db_path.exists()
    conn = sqlite3.connect(db_path)
    if created:
        os.chmod(db_path, 0o600)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn
