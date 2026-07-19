"""Lot C : résolution tolérante des identifiants, dédup, onboarding."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.onboard import detect_shell_rc, history_status, write_api_key
from ghost.resolve import (
    ResolveError,
    resolve_candidate,
    resolve_skill,
    skills_for_candidate,
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "t.db")
    yield conn
    conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO candidates (id, kind, signature, status) "
        "VALUES (291, 'FAILURE_LOOP', 'sig', 'new')"
    )
    conn.execute(
        "INSERT INTO skills (id, candidate_id, slug, model, prompt_version, verdict) "
        "VALUES (3, 291, 'edit-stale-read-recovery', 'm', '1', 'SKILL')"
    )
    conn.execute(
        "INSERT INTO skills (id, candidate_id, slug, model, prompt_version, verdict) "
        "VALUES (16, 291, 'edit-file-modified-since-read', 'm', '1', 'SKILL')"
    )
    conn.commit()


# --------------------------------------------------------------------------
# resolve_skill


def test_resolve_skill_by_slug(db: sqlite3.Connection) -> None:
    _seed(db)
    assert resolve_skill(db, "edit-file-modified-since-read").id == 16


def test_resolve_skill_by_id(db: sqlite3.Connection) -> None:
    _seed(db)
    r = resolve_skill(db, "16")
    assert r.id == 16 and r.note == ""


def test_resolve_skill_candidate_with_multiple_skills_is_ambiguous(
    db: sqlite3.Connection,
) -> None:
    _seed(db)
    # 291 est un candidat AVEC 2 skills → ambigu, message actionnable.
    with pytest.raises(ResolveError, match="plusieurs skills"):
        resolve_skill(db, "291")


def test_resolve_skill_candidate_single_skill_notes_translation(
    db: sqlite3.Connection,
) -> None:
    db.execute(
        "INSERT INTO candidates (id, kind, signature, status) "
        "VALUES (7, 'HUMAN_OVERRIDE', 's', 'new')"
    )
    db.execute(
        "INSERT INTO skills (id, candidate_id, slug, model, prompt_version, verdict) "
        "VALUES (42, 7, 'only-one', 'm', '1', 'SKILL')"
    )
    db.commit()
    r = resolve_skill(db, "7")  # candidat 7 → skill 42
    assert r.id == 42 and "candidat" in r.note


def test_resolve_skill_unknown_is_actionable(db: sqlite3.Connection) -> None:
    with pytest.raises(ResolveError, match="ghost skills"):
        resolve_skill(db, "nope-nope")


# --------------------------------------------------------------------------
# resolve_candidate


def test_resolve_candidate_by_id(db: sqlite3.Connection) -> None:
    _seed(db)
    assert resolve_candidate(db, "291").id == 291


def test_resolve_candidate_by_skill_id_translates(db: sqlite3.Connection) -> None:
    _seed(db)
    r = resolve_candidate(db, "16")  # skill 16 → candidat 291
    assert r.id == 291 and "skill" in r.note


def test_resolve_candidate_by_slug(db: sqlite3.Connection) -> None:
    _seed(db)
    assert resolve_candidate(db, "edit-stale-read-recovery").id == 291


def test_resolve_candidate_unknown(db: sqlite3.Connection) -> None:
    with pytest.raises(ResolveError):
        resolve_candidate(db, "12345")


# --------------------------------------------------------------------------
# dédup


def test_skills_for_candidate_lists_actives(db: sqlite3.Connection) -> None:
    _seed(db)
    assert skills_for_candidate(db, 291) == [
        (3, "edit-stale-read-recovery"),
        (16, "edit-file-modified-since-read"),
    ]
    # Désactiver l'ancien → n'apparaît plus comme doublon actif.
    db.execute("UPDATE skills SET disabled = 1 WHERE id = 3")
    db.commit()
    assert skills_for_candidate(db, 291) == [(16, "edit-file-modified-since-read")]


# --------------------------------------------------------------------------
# onboarding


def test_write_api_key_is_chmod_600(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "api_key"
    path = write_api_key("sk-ant-xyz  ", path=target)
    assert path.read_text(encoding="utf-8") == "sk-ant-xyz\n"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_history_status_virgin_and_populated(tmp_path: Path) -> None:
    assert history_status(tmp_path / "absent") == history_status(tmp_path / "absent")
    assert not history_status(tmp_path / "absent").projects_exist
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    (root / "p" / "s.jsonl").write_text("{}\n", encoding="utf-8")
    hs = history_status(root)
    assert hs.projects_exist and hs.n_files == 1


def test_detect_shell_rc_returns_path() -> None:
    assert isinstance(detect_shell_rc(), Path)
