"""Tests lot 7 : doctor, télémétrie (redaction/opt-in), kill switch."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.doctor import run_doctor
from ghost.manage import disable_skill, uninstall_skills, why_last
from ghost.telemetry import (
    CONFIG_FILE,
    TelemetryConfig,
    build_payload,
    classify_error,
    safe_command_family,
)
from tests.test_detect import _SEQ, ev, tool_call


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    _SEQ.clear()
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


# --------------------------------------------------------------------------
# Doctor


def test_doctor_reports_missing_history_and_empty_db(tmp_path: Path) -> None:
    checks = run_doctor(root=tmp_path / "absent", db=tmp_path / "pas.db")
    by_label = {c.label: c for c in checks}
    assert not by_label["historique Claude Code"].ok
    assert by_label["historique Claude Code"].fix  # dit quoi faire
    assert not by_label["base ~/.ghost/ghost.db"].ok
    assert by_label["base ~/.ghost/ghost.db"].fix
    # Écriture dans un tmpdir existant : OK.
    assert by_label["écriture ~/.ghost/"].ok


def test_doctor_low_session_count_warns(db: sqlite3.Connection, tmp_path: Path) -> None:
    tool_call(db, "s1", "Bash", "t1", result="ok")
    db.execute("UPDATE sessions SET started_at='2026-07-01', ended_at='2026-07-01'")
    db.commit()
    checks = run_doctor(root=tmp_path, db=Path(db.execute("PRAGMA database_list")
                        .fetchall()[0][2]))
    base = next(c for c in checks if c.label == "base ~/.ghost/ghost.db")
    assert not base.ok  # 1 session < 3
    assert "sessions" in base.fix


# --------------------------------------------------------------------------
# Télémétrie


def test_telemetry_config_roundtrip_and_default_off(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.json"
    assert TelemetryConfig.load(path).enabled is False  # off par défaut
    cfg = TelemetryConfig(enabled=True, endpoint="https://x.example/collect")
    cfg.save(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    reloaded = TelemetryConfig.load(path)
    assert reloaded.enabled and reloaded.endpoint == "https://x.example/collect"
    assert reloaded.install_id  # généré


def test_telemetry_payload_leaks_nothing_textual(db: sqlite3.Connection) -> None:
    # Cas hostiles reproduits par la revue : secrets en casse mixte, mots de
    # passe, noms de fichiers/scripts/projets nus.
    ev(db, "s1", role="user", block="text", human=1,
       text="SECRET PROMPT: corrige /Users/moi/projet-orion/app.py avec sk-ant-xyz")
    tool_call(db, "s1", "Bash", "t1", err=1,
              result="Invalid API key: sk_live_AbCdEf12345678\n"
                     "FATAL: password authentication failed for user acme_admin",
              payload={"input": {"command": "./deploy_projet_orion.sh --prod"}})
    tool_call(db, "s1", "Bash", "t2", err=1,
              result="Exit code 1\nModuleNotFoundError: No module named 'x' in /Users/moi/x.py",
              payload={"input": {"command": "uv run pytest tests/secret_test.py"}})
    tool_call(db, "s1", "Bash", "t3",
              payload={"input": {"command": "python migrate_acme_clients.py"}})
    ev(db, "s1", block="tool_use", tool="Edit", tuid="e1",
       payload={"input": {"file_path": "/Users/moi/projet-orion/app.py"}})
    db.execute(
        "INSERT INTO files_touched (event_id, path, op) "
        "SELECT MAX(id), '/Users/moi/projet-orion/app.py', 'edit' FROM events"
    )
    db.commit()
    payload = build_payload(db, TelemetryConfig(install_id="abc"), "0.1.0")
    blob = json.dumps(payload.to_dict())
    # RIEN de textuel sensible : ni fichiers, ni scripts, ni secrets, ni projet.
    for leak in ("SECRET PROMPT", "/Users/moi", "app.py", "secret_test",
                 "sk-ant", "sk_live", "acme", "projet-orion", "deploy_projet",
                 "migrate_acme", "password", "hunter"):
        assert leak not in blob, f"fuite : {leak}"
    # Allowlist : familles réduites aux verbes, erreurs classifiées.
    assert payload.languages.get("python") == 1
    assert payload.command_families.get("bash-uv") == 1  # uv run pytest → tête uv
    assert payload.command_families.get("bash-other") == 1  # ./deploy_x.sh
    assert payload.command_families.get("bash-python") == 1  # python <fichier> → tête python
    assert payload.error_classes.get("module_not_found") == 1


def test_telemetry_endpoint_validation() -> None:
    from ghost.telemetry import validate_endpoint

    assert validate_endpoint("https://collect.example/x")[0]
    assert validate_endpoint("http://localhost:8000/x")[0]  # local toléré
    assert not validate_endpoint("http://collect.example/x")[0]  # http distant refusé
    assert not validate_endpoint("ftp://nope")[0]


def test_telemetry_send_rejects_bad_endpoint() -> None:
    from ghost.telemetry import Payload, send

    payload = Payload(install_id="x", ghost_version="0", sent_at="now",
                      n_sessions=0, n_events=0)
    ok, detail = send(payload, "http://evil.example/x")
    assert not ok and "http" in detail.lower()


# --------------------------------------------------------------------------
# Kill switch


def _deploy_skill(conn: sqlite3.Connection, tmp_path: Path, sid: str) -> tuple[int, Path]:
    conn.execute(
        "INSERT INTO candidates (kind, signature, status) "
        "VALUES ('FAILURE_LOOP', 'sig', 'kept')"
    )
    cid = int(conn.execute("SELECT MAX(id) FROM candidates").fetchone()[0])
    conn.execute(
        "INSERT INTO skills (candidate_id, slug, path, model, prompt_version, verdict)"
        " VALUES (?, 'mon-skill', '/tmp/src', 'm', '1', 'SKILL')",
        (cid,),
    )
    skid = int(conn.execute("SELECT MAX(id) FROM skills").fetchone()[0])
    # Chemin confiné réaliste : …/.claude/skills/<slug>/SKILL.md
    deployed = tmp_path / ".claude" / "skills" / "mon-skill" / "SKILL.md"
    deployed.parent.mkdir(parents=True)
    deployed.write_text("---\nname: mon-skill\ndescription: d.\n---\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO deployments (skill_id, target_path, deployed_at) VALUES (?, ?, 'now')",
        (skid, str(deployed)),
    )
    conn.commit()
    return skid, deployed


def test_disable_removes_file_and_blocks_redeploy(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    skid, deployed = _deploy_skill(db, tmp_path, "s1")
    assert deployed.exists()
    removed, refused = disable_skill(db, skid)
    assert deployed in removed and not deployed.exists() and refused == []
    assert not deployed.parent.exists()  # répertoire de slug vidé nettoyé
    assert deployed.parent.parent.exists()  # …/skills/ conservé
    assert db.execute("SELECT disabled FROM skills WHERE id = ?", (skid,)).fetchone()[0] == 1
    from ghost.deploy import plan_deploy

    assert plan_deploy(db) == []
    # enable le rend redéployable.
    from ghost.manage import enable_skill

    enable_skill(db, skid)
    assert db.execute("SELECT disabled FROM skills WHERE id = ?", (skid,)).fetchone()[0] == 0


def test_disable_refuses_path_outside_skills_dir(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    # target_path pointant hors .claude/skills/ (corruption / mauvais deploy) :
    # jamais supprimé.
    db.execute(
        "INSERT INTO candidates (kind, signature, status) "
        "VALUES ('FAILURE_LOOP', 's', 'kept')"
    )
    cid = int(db.execute("SELECT MAX(id) FROM candidates").fetchone()[0])
    db.execute(
        "INSERT INTO skills (candidate_id, slug, path, model, prompt_version, verdict)"
        " VALUES (?, 'x', '/tmp/s', 'm', '1', 'SKILL')",
        (cid,),
    )
    skid = int(db.execute("SELECT MAX(id) FROM skills").fetchone()[0])
    victim = tmp_path / "important.txt"  # PAS un SKILL.md sous .claude/skills
    victim.write_text("données de l'utilisateur", encoding="utf-8")
    db.execute(
        "INSERT INTO deployments (skill_id, target_path, deployed_at) VALUES (?, ?, 'now')",
        (skid, str(victim)),
    )
    db.commit()
    removed, refused = disable_skill(db, skid)
    assert removed == [] and victim in refused
    assert victim.exists()  # fichier de l'utilisateur INTACT


def test_why_shows_injectable_deployed_skills(db: sqlite3.Connection, tmp_path: Path) -> None:
    skid, _ = _deploy_skill(db, tmp_path, "s1")
    ev(db, "s1", role="system", block="skill_listing",
       text="- mon-skill: Quand tu testes.\n- autre-builtin: x")
    db.execute("UPDATE sessions SET started_at = '2026-07-17T10:00:00Z' WHERE id = 's1'")
    db.commit()
    session_id, injected = why_last(db)
    assert session_id == "s1"
    slugs = {i.slug for i in injected}
    assert "mon-skill" in slugs and "autre-builtin" not in slugs
    mine = next(i for i in injected if i.slug == "mon-skill")
    assert mine.skill_id == skid and "testes" in mine.description


def test_uninstall_removes_all(db: sqlite3.Connection, tmp_path: Path) -> None:
    _deploy_skill(db, tmp_path, "s1")
    removed, refused = uninstall_skills(db)
    assert len(removed) == 1 and refused == []
    assert db.execute("SELECT COUNT(*) FROM deployments").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM skills WHERE disabled = 1").fetchone()[0] == 1


def test_classifiers_are_allowlist_only() -> None:
    assert safe_command_family("git commit -m x") == "bash-git"
    assert safe_command_family("./secret_deploy_client.sh") == "bash-other"
    assert safe_command_family("python migrate_acme.py") == "bash-python"
    assert classify_error("<tool_use_error>File has been modified since read") == "file_stale"
    assert classify_error("ModuleNotFoundError: No module named 'x'") == "module_not_found"
    assert classify_error("Invalid API key: sk_live_AbCd1234") == "other"


def test_config_file_path_is_under_ghost_home() -> None:
    assert CONFIG_FILE.parent.name == ".ghost"
