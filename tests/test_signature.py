"""Signature de tâche : la dominante d'un candidat = la clé de `ghost retrieve`
(et donc de `ghost publish`), au format task_signature — jamais la signature de
détecteur du candidat."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.signature import dominant_task_signature, task_signature
from tests.test_detect import _SEQ, tool_call


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    _SEQ.clear()
    conn = connect(tmp_path / "sig.db")
    yield conn
    conn.close()


def test_dominant_task_signature_picks_most_common(db: sqlite3.Connection) -> None:
    # s1, s2 : même classe de tâche (Bash uv run pytest) ; s3 : différente (Read).
    for s in ("s1", "s2"):
        tool_call(db, s, "Bash", f"{s}-t1", payload={"command": "uv run pytest"})
    tool_call(db, "s3", "Read", "s3-t1", payload={"file_path": "a.py"})
    db.execute(
        "INSERT INTO candidates (kind, signature, session_ids_json) VALUES (?, ?, ?)",
        ("FAILURE_LOOP", "Bash|detector-style", json.dumps(["s1", "s2", "s3"])),
    )
    cid = int(db.execute("SELECT id FROM candidates").fetchone()[0])

    dom = dominant_task_signature(db, cid)
    assert dom == task_signature(db, "s1")  # la classe dominante (2 contre 1)
    assert dom.count("|") == 3  # format task_signature : 4 parties
    assert dom != "Bash|detector-style"  # PAS la signature de détecteur


def test_dominant_empty_when_candidate_has_no_sessions(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO candidates (kind, signature, session_ids_json) VALUES (?, ?, ?)",
        ("FAILURE_LOOP", "X|y", "[]"),
    )
    cid = int(db.execute("SELECT id FROM candidates").fetchone()[0])
    assert dominant_task_signature(db, cid) == ""
