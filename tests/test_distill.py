"""Tests lot 3 : redaction, trace, validation structurelle, distillation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.distill import distill, validate_structure
from ghost.redact import redact
from ghost.scan import run_scan
from ghost.trace import build_trace
from tests.test_detect import _SEQ, tool_call


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    _SEQ.clear()
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


# --------------------------------------------------------------------------
# Redaction


def test_redact_api_keys_and_tokens(tmp_path: Path) -> None:
    text = (
        "clé sk-ant-abc123def456ghi789 et token ghp_abcdefghij1234567890 "
        "et jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123"
    )
    clean, counts = redact(text, deny_file=tmp_path / "absent.txt")
    assert "sk-ant-" not in clean
    assert "ghp_" not in clean
    assert "eyJ" not in clean
    assert counts["api_key"] == 2
    assert counts["jwt"] == 1


def test_redact_url_creds_and_env(tmp_path: Path) -> None:
    text = (
        "DATABASE_URL: postgres://admin:s3cretpass@db.example.com/prod\n"
        "export STRIPE_SECRET_KEY=sk_live_abcdefghijklmnop\n"
    )
    clean, counts = redact(text, deny_file=tmp_path / "absent.txt")
    assert "s3cretpass" not in clean
    assert "abcdefghijklmnop" not in clean
    assert counts["url_creds"] == 1


def test_redact_home_email_and_deny_list(tmp_path: Path) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("NomDeClientSensible\n# commentaire\n", encoding="utf-8")
    text = f"fichier {Path.home()}/proj/x.py de jordan@example.com chez NomDeClientSensible"
    clean, counts = redact(text, deny_file=deny)
    assert str(Path.home()) not in clean
    assert "jordan@example.com" not in clean
    assert "NomDeClientSensible" not in clean
    assert counts["deny_list"] == 1 and counts["email"] == 1 and counts["home_path"] == 1


def test_redact_fail_closed_on_unreadable_deny(tmp_path: Path) -> None:
    from ghost.redact import RedactionError

    deny_as_dir = tmp_path / "deny.txt"
    deny_as_dir.mkdir()  # présent mais illisible en tant que fichier
    with pytest.raises(RedactionError):
        redact("PROJECT-CHIMERA-launchcode-42", deny_file=deny_as_dir)


def test_redact_deny_indented_comment_ignored(tmp_path: Path) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("  # commentaire indenté\nVraiSecret\n", encoding="utf-8")
    clean, _ = redact("# commentaire indenté et VraiSecret", deny_file=deny)
    assert "# commentaire indenté" in clean  # pas devenu un littéral de deny-list
    assert "VraiSecret" not in clean


def test_redact_pem_and_midline_env_secret(tmp_path: Path) -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef456\n-----END RSA PRIVATE KEY-----\n"
        "[12] outil Bash: export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI1234\n"
        'config: {"api_key": "abcd1234efgh"}\n'
        "Authorization: token ghs_notaprefixmatch12345\n"
        "https://x.com/cb?access_token=ya29abc123def456&x=1"
    )
    clean, counts = redact(text, deny_file=tmp_path / "absent.txt")
    assert "MIIEabc" not in clean
    assert "wJalrXUtnFEMI1234" not in clean  # env_secret non ancré en début de ligne
    assert "abcd1234efgh" not in clean  # "api_key": "…" en JSON
    assert "ghs_notaprefixmatch12345" not in clean  # Authorization: token
    assert "ya29abc123def456" not in clean  # token en query param
    assert counts["private_key"] == 1


# --------------------------------------------------------------------------
# Trace


def _make_candidate(conn: sqlite3.Connection) -> int:
    s = "s1"
    tool_call(conn, s, "Bash", "t1", err=1, result="Exit code 1\nModuleNotFoundError: gw_kernel")
    tool_call(conn, s, "Bash", "t2", err=1, result="Exit code 1\nModuleNotFoundError: gw_kernel")
    tool_call(conn, s, "Bash", "t3", result="ok tests verts")
    run_scan(conn)
    row = conn.execute("SELECT id FROM candidates WHERE kind = 'FAILURE_LOOP'").fetchone()
    assert row is not None
    return int(row[0])


def test_build_trace_keeps_full_errors(db: sqlite3.Connection) -> None:
    cid = _make_candidate(db)
    trace = build_trace(db, cid)
    assert "ModuleNotFoundError: gw_kernel" in trace.text
    assert "❌ ERREUR" in trace.text
    assert trace.n_occurrences == 1
    assert trace.est_tokens > 0


def test_build_trace_unknown_candidate(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        build_trace(db, 99999)


def test_build_trace_keeps_error_tail(db: sqlite3.Connection) -> None:
    s = "s1"
    long_error = "Exit code 1\n" + ("ligne de bruit\n" * 500) + "FINAL_MARKER_DE_TRACEBACK"
    tool_call(db, s, "Bash", "t1", err=1, result=long_error)
    tool_call(db, s, "Bash", "t2", err=1, result=long_error)
    tool_call(db, s, "Bash", "t3", result="ok")
    run_scan(db)
    row = db.execute("SELECT id FROM candidates WHERE kind = 'FAILURE_LOOP'").fetchone()
    trace = build_trace(db, int(row[0]))
    # La queue du traceback (la ligne décisive) survit à la troncature.
    assert "FINAL_MARKER_DE_TRACEBACK" in trace.text


def test_build_trace_survives_stale_event_ids(db: sqlite3.Connection) -> None:
    # Ré-ingestion simulée : les rowids changent, les (src_file, src_line)
    # restent — la trace doit se résoudre via src_refs.
    cid = _make_candidate(db)
    rows = db.execute(
        "SELECT session_id, agent_id, seq, ts, role, block_type, tool_name, tool_use_id,"
        " is_error, is_human, text, payload_json, src_file, src_line FROM events"
    ).fetchall()
    db.execute("DELETE FROM events")
    for r in rows:  # réinsertion avec des ids décalés
        db.execute(
            "INSERT INTO events (id, session_id, agent_id, seq, ts, role, block_type,"
            " tool_name, tool_use_id, is_error, is_human, text, payload_json, src_file,"
            " src_line) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            r,
        )
    db.commit()
    trace = build_trace(db, cid)
    assert trace.n_occurrences == 1
    assert "ModuleNotFoundError" in trace.text


# --------------------------------------------------------------------------
# Validation structurelle


def _valid_skill_data() -> dict[str, object]:
    return {
        "decision": "SKILL",
        "skip_reason": "",
        "name": "monorepo-pythonpath",
        "description": "Quand un import échoue hors pytest dans le monorepo.",
        "tags": ["python"],
        "stack": ["uv"],
        "quand_utiliser": "Import direct d'un package du monorepo.",
        "procedure": ["Utiliser uv run pytest, jamais python -c."],
        "pieges": [
            {
                "piege": "Les imports ad hoc échouent : seul le conftest injecte sys.path.",
                "preuve": "[12] ❌ ERREUR: ModuleNotFoundError: gw_kernel après 3 tentatives",
            }
        ],
        "anti_patterns": ["Hacker sys.path à la main."],
    }


def test_validate_structure_accepts_valid_and_skip() -> None:
    assert validate_structure(_valid_skill_data()) == []
    assert validate_structure({"decision": "SKIP", "skip_reason": "rien de non-évident"}) == []
    assert validate_structure({"decision": "SKIP", "skip_reason": ""}) != []


def test_validate_structure_rejects_empty_pieges() -> None:
    data = _valid_skill_data()
    data["pieges"] = []
    assert any("Pièges vide" in p for p in validate_structure(data))
    data["pieges"] = [{"piege": "x", "preuve": ""}]
    assert validate_structure(data) != []


# --------------------------------------------------------------------------
# Distillation (LLM factice)


def test_distill_writes_skill_and_records(db: sqlite3.Connection, tmp_path: Path) -> None:
    cid = _make_candidate(db)
    calls: list[str] = []

    def fake_caller(
        system: str, user: str, schema: dict[str, object], max_tokens: int
    ) -> tuple[dict[str, object], int, int]:
        calls.append(user[:40])
        if "Ce skill contient-il" in user:
            return {"verdict": "OUI", "ligne": "seul le conftest injecte sys.path"}, 500, 50
        assert "ModuleNotFoundError" in user  # la trace est bien dans le prompt
        return _valid_skill_data(), 8000, 900

    result = distill(db, cid, caller=fake_caller, skills_dir=tmp_path / "skills")
    assert result.verdict == "SKILL"
    assert result.skill_path is not None and result.skill_path.exists()
    md = result.skill_path.read_text(encoding="utf-8")
    assert "## Pièges" in md and "Preuve :" in md
    assert len(calls) == 2  # distillation + auto-critique
    assert not result.low_value
    assert 0 < result.cost_usd < 0.20

    row = db.execute(
        "SELECT verdict, low_value, cost_usd FROM skills WHERE candidate_id = ?", (cid,)
    ).fetchone()
    assert row is not None and row[0] == "SKILL"
    status = db.execute("SELECT status FROM candidates WHERE id = ?", (cid,)).fetchone()
    assert status[0] == "distilled"


def test_distill_skip_and_structural_downgrade(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    cid = _make_candidate(db)

    def skip_caller(
        system: str, user: str, schema: dict[str, object], max_tokens: int
    ) -> tuple[dict[str, object], int, int]:
        return {"decision": "SKIP", "skip_reason": "échec transitoire, rien à apprendre"}, 5000, 100

    result = distill(db, cid, caller=skip_caller, skills_dir=tmp_path / "skills")
    assert result.verdict == "SKIP" and result.skill_path is None

    def invalid_skill_caller(
        system: str, user: str, schema: dict[str, object], max_tokens: int
    ) -> tuple[dict[str, object], int, int]:
        data = _valid_skill_data()
        data["pieges"] = []  # SKILL sans pièges → rejet structurel
        return data, 5000, 100

    result2 = distill(db, cid, caller=invalid_skill_caller, skills_dir=tmp_path / "skills")
    assert result2.verdict == "SKIP"
    assert result2.skip_reason is not None and "rejet structurel" in result2.skip_reason


def test_distill_slug_dedup(db: sqlite3.Connection, tmp_path: Path) -> None:
    cid = _make_candidate(db)

    def caller(
        system: str, user: str, schema: dict[str, object], max_tokens: int
    ) -> tuple[dict[str, object], int, int]:
        if "Ce skill contient-il" in user:
            return {"verdict": "OUI", "ligne": "x"}, 100, 10
        return _valid_skill_data(), 1000, 100

    r1 = distill(db, cid, caller=caller, skills_dir=tmp_path / "skills")
    r2 = distill(db, cid, caller=caller, skills_dir=tmp_path / "skills")
    assert r1.slug == "monorepo-pythonpath"
    assert r2.slug == "monorepo-pythonpath-2"
