"""Tests lot 5 : skill_listing, usage dédupliqué, signature, cohortes watch."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.parse import parse_line
from ghost.signature import task_signature
from ghost.watch import collect, matched_classes, render
from tests.test_detect import _SEQ, ev, tool_call


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    _SEQ.clear()
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


# --------------------------------------------------------------------------
# Parse : skill_listing + usage


def test_parse_skill_listing_attachment() -> None:
    obj = {
        "type": "attachment",
        "attachment": {
            "type": "skill_listing",
            "content": "- edit-stale-read-recovery: Quand un Edit échoue.\n- autre: x",
        },
        "timestamp": "2026-07-17T10:00:00.000Z",
    }
    blocks = parse_line(obj, sidechain=False)
    assert len(blocks) == 1
    assert blocks[0].block_type == "skill_listing"
    assert "edit-stale-read-recovery" in str(blocks[0].text)
    # Les autres attachments restent ignorés.
    assert parse_line(
        {"type": "attachment", "attachment": {"type": "deferred_tools_delta"}},
        sidechain=False,
    ) == []


def test_parse_usage_on_first_block_only() -> None:
    obj = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "id": "msg_1",
            "usage": {"output_tokens": 42},
            "content": [
                {"type": "thinking", "thinking": "x", "signature": "s"},
                {"type": "text", "text": "réponse"},
            ],
        },
    }
    blocks = parse_line(obj, sidechain=False)
    assert blocks[0].usage_out == 42
    assert blocks[1].usage_out is None


# --------------------------------------------------------------------------
# Signature


def _seed_pytest_session(conn: sqlite3.Connection, sid: str, *, commit: bool) -> None:
    tool_call(conn, sid, "Bash", f"{sid}-t1",
              payload={"input": {"command": "uv run pytest -q"}})
    ev(conn, sid, block="tool_use", tool="Edit", tuid=f"{sid}-t2",
       payload={"input": {"file_path": "/p/api/main.py"}})
    conn.execute(
        "INSERT INTO files_touched (event_id, path, op) "
        "SELECT MAX(id), '/p/api/main.py', 'edit' FROM events"
    )
    ev(conn, sid, role="user", block="tool_result", tuid=f"{sid}-t2", err=1,
       text="Exit code 1\nModuleNotFoundError: No module named 'x'")
    if commit:
        tool_call(conn, sid, "Bash", f"{sid}-t3", result="[main abc] ok",
                  payload={"input": {"command": "git commit -m x"}})
    conn.commit()


def test_task_signature_groups_similar_sessions(db: sqlite3.Connection) -> None:
    _seed_pytest_session(db, "s1", commit=True)
    _seed_pytest_session(db, "s2", commit=True)
    sig1 = task_signature(db, "s1")
    sig2 = task_signature(db, "s2")
    assert sig1 == sig2
    assert "commit" in sig1 and "py" in sig1 and "modulenotfounderror" in sig1

    _seed_pytest_session(db, "s3", commit=False)
    assert task_signature(db, "s3") != sig1  # sans commit → autre classe


# --------------------------------------------------------------------------
# Watch : cohortes et appariement


def _add_listing(conn: sqlite3.Connection, sid: str, slugs: list[str]) -> None:
    content = "\n".join(f"- {s}: desc" for s in slugs)
    ev(conn, sid, role="system", block="skill_listing", text=content)


def _register_deploy(conn: sqlite3.Connection, slug: str, when: str) -> None:
    conn.execute(
        "INSERT INTO candidates (kind, signature, status) VALUES ('FAILURE_LOOP', ?, 'kept')",
        (slug,),
    )
    cid = conn.execute("SELECT MAX(id) FROM candidates").fetchone()[0]
    conn.execute(
        "INSERT INTO skills (candidate_id, slug, path, model, prompt_version, verdict)"
        " VALUES (?, ?, '/tmp/x', 'm', '1', 'SKILL')",
        (cid, slug),
    )
    skid = conn.execute("SELECT MAX(id) FROM skills").fetchone()[0]
    conn.execute(
        "INSERT INTO deployments (skill_id, target_path, deployed_at) VALUES (?, '/t', ?)",
        (skid, when),
    )
    conn.commit()


def test_watch_cohorts_and_matching(db: sqlite3.Connection) -> None:
    deploy_at = "2026-07-10T00:00:00+00:00"
    _register_deploy(db, "mon-skill", deploy_at)

    # Baseline : avant deploy (même si le slug est listé — garde
    # temporelle). Exposée : slug listé APRÈS deploy, thread principal.
    # Post non exposée : après deploy sans slug, ou slug listé seulement
    # dans un subagent.
    for sid, ts, listed in (
        ("b1", "2026-07-01T10:00:00.000Z", []),
        ("b2", "2026-07-05T10:00:00.000Z", ["autre-skill"]),
        ("b3", "2026-07-05T12:00:00.000Z", ["mon-skill"]),  # pré-deploy
        ("e1", "2026-07-12T10:00:00.000Z", ["mon-skill"]),
        ("p1", "2026-07-12T11:00:00.000Z", ["autre-skill"]),
    ):
        _seed_pytest_session(db, sid, commit=True)
        if listed:
            _add_listing(db, sid, listed)
        db.execute("UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
                   (ts, ts.replace(":00:00.000Z", ":30:00.000Z"), sid))
    # p2 : listing du slug déployé mais UNIQUEMENT dans un thread subagent.
    _seed_pytest_session(db, "p2", commit=True)
    ev(db, "p2", role="system", block="skill_listing",
       text="- mon-skill: desc", agent="a1")
    db.execute("UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
               ("2026-07-12T12:00:00.000Z", "2026-07-12T12:30:00.000Z", "p2"))
    db.commit()

    report = collect(db)
    cohorts = {s.session_id: s.cohort for s in report.sessions}
    assert cohorts == {
        "b1": "baseline", "b2": "baseline", "b3": "baseline",
        "e1": "exposee", "p1": "post_non_exposee", "p2": "post_non_exposee",
    }
    matched, _ = matched_classes(report)
    assert len(matched) == 1  # même signature partout → une classe appariée
    assert len(matched[0].baseline) == 3 and len(matched[0].exposed) == 1

    out = render(report)
    assert "Classe:" in out and "n insuffisant" in out  # n exposées = 1 < 3


def test_watch_tokens_cumulative_max_per_message(db: sqlite3.Connection) -> None:
    _seed_pytest_session(db, "s1", commit=False)
    # Deux snapshots cumulatifs du même message (10 puis 42) + un autre
    # message (7) : la session vaut 42 + 7, pas 10 + 42 + 7.
    for usage, mid in ((10, "msg_a"), (42, "msg_a"), (7, "msg_b")):
        db.execute(
            "INSERT INTO events (session_id, seq, role, block_type, is_error,"
            " is_human, usage_out, msg_id, src_file, src_line)"
            " VALUES ('s1', 999, 'assistant', 'text', 0, 0, ?, ?, 'x', 1)",
            (usage, mid),
        )
    db.commit()
    report = collect(db)
    metrics = next(s for s in report.sessions if s.session_id == "s1")
    assert metrics.tokens_out == 49


def test_ingest_rebuild_aborts_on_missing_files(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ghost.cli import app

    db_path = tmp_path / "ghost.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO ingest_log (path, mtime, sha, ingested_at)"
        " VALUES (?, 0, 'x', 'now')",
        (str(tmp_path / "disparu.jsonl"),),
    )
    conn.commit()
    conn.close()
    result = CliRunner().invoke(
        app,
        ["ingest", "--rebuild", "--db", str(db_path), "--root", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "disparu" in result.output or "PERDRAIT" in result.output


def test_watch_no_match_when_no_exposed(db: sqlite3.Connection) -> None:
    _register_deploy(db, "mon-skill", "2026-07-10T00:00:00+00:00")
    _seed_pytest_session(db, "b1", commit=True)
    report = collect(db)
    matched, excluded = matched_classes(report)
    assert matched == [] and len(excluded) == 1
    assert "Aucune classe appariée" in render(report)
