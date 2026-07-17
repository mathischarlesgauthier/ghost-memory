"""Tests d'ingestion sur fixtures anonymisées calquées sur le format réel."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.ingest import FileResult, SourceFile, ingest_all

FIXTURES = Path(__file__).parent / "fixtures" / "projects"

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES, root)
    return root, tmp_path / "ghost.db"


def run_ingest(root: Path, db: Path) -> list[tuple[SourceFile, FileResult]]:
    conn = connect(db)
    try:
        return list(ingest_all(conn, root))
    finally:
        conn.close()


def query(db: Path, sql: str, *params: object) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def test_counts_and_idempotence(corpus: tuple[Path, Path]) -> None:
    root, db = corpus
    results = run_ingest(root, db)
    assert all(r.status == "ingested" for _, r in results)
    assert query(db, "SELECT COUNT(*) FROM events")[0][0] == 17
    assert query(db, "SELECT COUNT(*) FROM sessions")[0][0] == 2
    # Le journal.jsonl des workflows (orchestration) n'est pas un transcript.
    assert query(db, "SELECT COUNT(*) FROM events WHERE src_file LIKE '%journal%'")[0][0] == 0

    # 2e run : tout est inchangé, aucun event dupliqué.
    results2 = run_ingest(root, db)
    assert all(r.status == "unchanged" for _, r in results2)
    assert query(db, "SELECT COUNT(*) FROM events")[0][0] == 17
    assert query(db, "SELECT COUNT(*) FROM files_touched")[0][0] == 3


def test_corrupted_line_skipped(corpus: tuple[Path, Path]) -> None:
    root, db = corpus
    results = run_ingest(root, db)
    by_session = {src.session_id: res for src, res in results if src.agent_id is None}
    assert by_session[S2].status == "ingested"
    assert by_session[S2].n_skipped_lines == 1
    # Les lignes valides autour de la ligne corrompue sont bien ingérées.
    assert query(db, "SELECT COUNT(*) FROM events WHERE session_id = ?", S2)[0][0] == 3


def test_path_extraction(corpus: tuple[Path, Path]) -> None:
    root, db = corpus
    run_ingest(root, db)
    rows = query(
        db,
        "SELECT f.path, f.op FROM files_touched f "
        "JOIN events e ON e.id = f.event_id WHERE e.session_id = ?",
        S1,
    )
    assert set(rows) == {
        ("/Users/test/demoapp/app.py", "read"),
        ("/Users/test/demoapp/app.py", "edit"),
        ("/Users/test/demoapp/notes.md", "write"),
    }


def test_is_human_classification(corpus: tuple[Path, Path]) -> None:
    root, db = corpus
    run_ingest(root, db)
    human = query(
        db, "SELECT text FROM events WHERE is_human = 1 ORDER BY session_id, seq"
    )
    texts = [str(t[0]) for t in human]
    assert texts == [
        "corrige le bug d'authentification dans app.py",
        "ajoute un test pour le module payments",
    ]
    # La task-notification déguisée en user et le prompt du subagent
    # (sidechain) ne sont pas humains.
    assert query(
        db,
        "SELECT COUNT(*) FROM events WHERE is_human = 1 AND (agent_id IS NOT NULL "
        "OR text LIKE '<task-notification>%')",
    )[0][0] == 0


def test_error_flag(corpus: tuple[Path, Path]) -> None:
    root, db = corpus
    run_ingest(root, db)
    rows = query(
        db,
        "SELECT tool_use_id, is_error FROM events WHERE block_type = 'tool_result' "
        "AND session_id = ? ORDER BY seq",
        S1,
    )
    assert rows == [("toolu_01", 0), ("toolu_02", 1), ("toolu_03", 0)]


def test_subagent_and_session_metadata(corpus: tuple[Path, Path]) -> None:
    root, db = corpus
    run_ingest(root, db)
    assert query(
        db, "SELECT session_id, agent_type, tool_use_id FROM agents WHERE agent_id = ?",
        "abc123def456",
    ) == [(S1, "general-purpose", "toolu_09")]
    # L'agent de workflow (imbriqué sous subagents/workflows/wf_*/) est là aussi.
    assert query(
        db, "SELECT session_id, agent_type FROM agents WHERE agent_id = ?", "def789fedcba"
    ) == [(S1, "workflow-subagent")]
    # Les events du subagent portent agent_id et comptent dans la session.
    assert query(db, "SELECT COUNT(*) FROM events WHERE agent_id = ?", "abc123def456")[0][0] == 2
    rows = query(
        db,
        "SELECT title, claude_version, n_events, started_at, ended_at "
        "FROM sessions WHERE id = ?",
        S1,
    )
    assert rows == [
        ("fix auth bug", "2.1.200", 14, "2026-01-05T10:00:00.000Z", "2026-01-05T10:00:31.000Z")
    ]


def test_grown_file_reingested_without_duplicates(corpus: tuple[Path, Path]) -> None:
    root, db = corpus
    run_ingest(root, db)
    target = root / "-Users-test-otherproj" / f"{S2}.jsonl"
    new_line = (
        '{"parentUuid":"u2","isSidechain":false,"type":"assistant",'
        '"message":{"role":"assistant","content":[{"type":"text","text":"Commit fait."}]},'
        f'"uuid":"a2","timestamp":"2026-02-10T09:02:00.000Z","sessionId":"{S2}"}}\n'
    )
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(new_line)
    stat = target.stat()
    os.utime(target, (stat.st_atime, stat.st_mtime + 10))

    results = run_ingest(root, db)
    statuses = {src.path.name: res.status for src, res in results}
    assert statuses[f"{S2}.jsonl"] == "ingested"
    assert query(db, "SELECT COUNT(*) FROM events WHERE session_id = ?", S2)[0][0] == 4
    assert query(db, "SELECT COUNT(*) FROM events")[0][0] == 18
    assert query(db, "SELECT ended_at FROM sessions WHERE id = ?", S2)[0][0] == (
        "2026-02-10T09:02:00.000Z"
    )
