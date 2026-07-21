"""Tests lot 6 : sandbox, sélection de cas, protocole, agrégation, lift."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.replay import ReplayError, RunMetrics, sandbox
from ghost.validate import (
    LiftReport,
    ReplayCase,
    aggregate,
    eligible_cases,
    run_validation,
    skill_info,
    write_lift_frontmatter,
)
from tests.test_detect import _SEQ, ev, tool_call


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    _SEQ.clear()
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


def _make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "vrai-repo"
    repo.mkdir()
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("v1\n")
    git("add", ".")
    git("commit", "-qm", "c1")
    c1 = git("rev-parse", "--short", "HEAD")
    (repo / "a.txt").write_text("v2\n")
    git("add", ".")
    git("commit", "-qm", "c2")
    c2 = git("rev-parse", "--short", "HEAD")
    return repo, c1, c2


# --------------------------------------------------------------------------
# Sandbox


def test_sandbox_isolated_clone_no_remote(tmp_path: Path) -> None:
    repo, c1, _c2 = _make_repo(tmp_path)
    with sandbox(repo, c1) as work:
        assert work != repo and work.exists()
        head = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert head == c1  # détaché sur le commit parent
        remotes = subprocess.run(
            ["git", "-C", str(work), "remote"], capture_output=True, text=True
        ).stdout.strip()
        assert remotes == ""  # push impossible par construction
        saved = work
    assert not saved.exists()  # cleanup garanti


def test_sandbox_cleanup_on_crash(tmp_path: Path) -> None:
    repo, c1, _c2 = _make_repo(tmp_path)
    saved = None
    with pytest.raises(RuntimeError, match="boom"), sandbox(repo, c1) as work:
        saved = work
        raise RuntimeError("boom")
    assert saved is not None and not saved.exists()


def test_sandbox_refuses_bad_input(tmp_path: Path) -> None:
    repo, c1, _ = _make_repo(tmp_path)
    with pytest.raises(ReplayError), sandbox(tmp_path / "pas-un-repo", c1):
        pass
    with pytest.raises(ReplayError), sandbox(repo, "0000000"):
        pass


# --------------------------------------------------------------------------
# Sélection des cas


def _seed_replayable_session(
    conn: sqlite3.Connection, sid: str, repo: Path, commit_hash: str
) -> None:
    ev(conn, sid, role="user", block="text", human=1,
       text="corrige le module de paiement et committe quand les tests passent ok")
    tool_call(conn, sid, "Bash", f"{sid}-t1", err=1,
              result="Exit code 1\nModuleNotFoundError: No module named 'pay'")
    tool_call(conn, sid, "Bash", f"{sid}-t2", err=1,
              result="Exit code 1\nModuleNotFoundError: No module named 'pay'")
    tool_call(conn, sid, "Bash", f"{sid}-t3", result="ok")
    tool_call(conn, sid, "Bash", f"{sid}-t4",
              result=f"[main {commit_hash}] fix pay",
              payload={"input": {"command": "git commit -m 'fix pay'"}})
    conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(repo), sid))
    conn.commit()


def _seed_skill_for(conn: sqlite3.Connection, session_ids: list[str]) -> int:
    import json as _json
    conn.execute(
        "INSERT INTO candidates (kind, signature, status, session_ids_json)"
        " VALUES ('FAILURE_LOOP', 'sig', 'kept', ?)",
        (_json.dumps(session_ids),),
    )
    cid = int(conn.execute("SELECT MAX(id) FROM candidates").fetchone()[0])
    skill_dir = Path(conn.execute("PRAGMA database_list").fetchall()[0][2]).parent / "sk"
    skill_dir.mkdir(exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        "---\nname: sk\ndescription: test.\ntags: []\nstack: []\n---\n\n## Pièges\n- x\n",
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO skills (candidate_id, slug, path, model, prompt_version, verdict)"
        " VALUES (?, 'sk', ?, 'm', '1', 'SKILL')",
        (cid, str(md)),
    )
    conn.commit()
    return int(conn.execute("SELECT MAX(id) FROM skills").fetchone()[0])


def test_eligible_cases_selection(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo, _c1, c2 = _make_repo(tmp_path)
    _seed_replayable_session(db, "s1", repo, c2)
    # s2 : même signature mais cwd sans repo git → exclue, comptée.
    _seed_replayable_session(db, "s2", tmp_path / "nulle-part", c2)
    skill_id = _seed_skill_for(db, ["s1"])
    skill = skill_info(db, skill_id)
    cases, counts = eligible_cases(db, skill)
    assert [c.session_id for c in cases] == ["s1"]
    assert cases[0].base_commit == f"{c2}~1"
    assert "committe" in cases[0].prompt
    assert counts["repo_ou_commit_absent"] == 1


def test_eligible_cases_souple_matches_by_shared_files(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    repo, _c1, c2 = _make_repo(tmp_path)
    _seed_replayable_session(db, "s1", repo, c2)
    # s3 : signature DIFFÉRENTE (pas d'erreur) mais touche le même fichier.
    ev(db, "s3", role="user", block="text", human=1,
       text="ajoute une option de configuration au module de paiement stp merci")
    ev(db, "s3", block="tool_use", tool="Edit", tuid="s3-e1",
       payload={"input": {"file_path": "/p/pay/core.py"}})
    db.execute(
        "INSERT INTO files_touched (event_id, path, op) "
        "SELECT MAX(id), '/p/pay/core.py', 'edit' FROM events"
    )
    tool_call(db, "s3", "Bash", "s3-t1", result=f"[main {c2}] add option",
              payload={"input": {"command": "git commit -m x"}})
    db.execute("UPDATE sessions SET cwd = ? WHERE id = 's3'", (str(repo),))
    # Le fichier commun avec s1 (session source du skill) :
    ev(db, "s1", block="tool_use", tool="Edit", tuid="s1-e9",
       payload={"input": {"file_path": "/p/pay/core.py"}})
    db.execute(
        "INSERT INTO files_touched (event_id, path, op) "
        "SELECT MAX(id), '/p/pay/core.py', 'edit' FROM events"
    )
    db.commit()
    skill_id = _seed_skill_for(db, ["s1"])
    skill = skill_info(db, skill_id)

    strict, _ = eligible_cases(db, skill, match="strict")
    assert {c.session_id for c in strict} == {"s1"}
    souple, _ = eligible_cases(db, skill, match="souple")
    assert {c.session_id for c in souple} == {"s1", "s3"}


# --------------------------------------------------------------------------
# Protocole


def _fake_case(case_id: str, repo: Path, base: str) -> ReplayCase:
    return ReplayCase(
        case_id=case_id, session_id=case_id, prompt="fais le travail",
        repo=repo, base_commit=base, head_end="x", signature="sig",
    )


def _metrics(turns: int, cost: float = 0.10, success: bool = True) -> RunMetrics:
    return RunMetrics(
        turns=turns, cost_usd=cost, duration_ms=1000, output_tokens=turns * 100,
        tool_errors=0, new_commits=1 if success else 0, changed_lines=5,
        success=success, is_error=False, denials=0,
    )


def test_run_validation_alternates_persists_and_stops_on_budget(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    repo, c1, _ = _make_repo(tmp_path)
    skill_id = _seed_skill_for(db, [])
    skill = skill_info(db, skill_id)
    order: list[str] = []

    @contextmanager
    def fake_sandbox(_repo: Path, _base: str) -> Iterator[Path]:
        yield tmp_path / "sandbox-factice"

    calls = {"n": 0}

    def fake_runner(work: Path, prompt: str, **_kw: object) -> RunMetrics:
        calls["n"] += 1
        return _metrics(turns=10, cost=0.50)

    def track(msg: str) -> None:
        order.append(msg)

    (tmp_path / "sandbox-factice" / ".claude" / "skills").mkdir(parents=True)
    report = run_validation(
        db, skill, [_fake_case("caseA", repo, c1)],
        max_cost_usd=2.0, n_per_condition=3,
        runner=fake_runner, sandbox_factory=fake_sandbox, on_progress=track,
    )
    # Budget 2.0, coût 0.5/run, marge 0.6 : runs tant que dépensé+0.6 <= 2.0
    # → 3 runs (1.5) puis arrêt (1.5+0.6 > 2.0).
    assert calls["n"] == 3
    assert report.stopped_on_budget
    # Ordre alterné sans/avec, jamais groupé.
    conditions = [msg.split("·")[1].strip() for msg in order if "case" in msg]
    assert conditions[:3] == ["sans", "avec", "sans"]
    # Persistance + reprise : relance → les runs faits ne sont pas rejoués.
    report2 = run_validation(
        db, skill, [_fake_case("caseA", repo, c1)],
        max_cost_usd=100.0, n_per_condition=3,
        runner=fake_runner, sandbox_factory=fake_sandbox,
    )
    assert calls["n"] == 6  # 3 restants seulement
    assert len(report2.records) == 3


# --------------------------------------------------------------------------
# Agrégation


def _insert_replay(
    conn: sqlite3.Connection, skill_id: int, case_id: str, condition: str,
    run_idx: int, turns: int, success: bool,
) -> None:
    import json as _json
    conn.execute(
        "INSERT INTO replays (skill_id, case_id, condition, run_idx, metrics_json,"
        " cost_usd) VALUES (?, ?, ?, ?, ?, 0.1)",
        (skill_id, case_id, condition, run_idx,
         _json.dumps({"turns": turns, "output_tokens": turns * 1000,
                      "tool_errors": 0, "duration_ms": 1000, "success": success})),
    )
    conn.commit()


def test_aggregate_detects_consistent_lift(db: sqlite3.Connection, tmp_path: Path) -> None:
    skill_id = _seed_skill_for(db, [])
    for case in ("c1", "c2", "c3"):
        for i, turns in enumerate((14, 12, 15)):
            _insert_replay(db, skill_id, case, "sans", i, turns, success=False)
        for i, turns in enumerate((6, 7, 6)):
            _insert_replay(db, skill_id, case, "avec", i, turns, success=True)
    report = aggregate(db, skill_id)
    assert report.n_cases == 3
    assert "lift" in report.verdict and "turns" in report.verdict
    assert report.success_sans == (0, 9) and report.success_avec == (9, 9)


def test_aggregate_flat_when_overlapping(db: sqlite3.Connection, tmp_path: Path) -> None:
    skill_id = _seed_skill_for(db, [])
    # c1 s'améliore, c2 empire → signes mixtes → pas de lift mesurable.
    for i, turns in enumerate((10, 11, 12)):
        _insert_replay(db, skill_id, "c1", "sans", i, turns, success=True)
        _insert_replay(db, skill_id, "c2", "avec", i, turns, success=True)
    for i, turns in enumerate((8, 8, 9)):
        _insert_replay(db, skill_id, "c1", "avec", i, turns, success=True)
        _insert_replay(db, skill_id, "c2", "sans", i, turns, success=True)
    report = aggregate(db, skill_id)
    assert report.verdict == "pas de lift mesurable"


def test_write_lift_frontmatter(tmp_path: Path) -> None:
    md = tmp_path / "SKILL.md"
    md.write_text(
        "---\nname: x\ndescription: d.\n---\n\n## Quand utiliser\nY.\n",
        encoding="utf-8",
    )
    report = LiftReport(skill_id=1, n_cases=3, n_runs=18, cost_usd=3.2)
    report.verdict = "lift -52% turns"
    write_lift_frontmatter(md, report)
    text = md.read_text(encoding="utf-8")
    assert "lift: lift -52% turns (n=3 cas, 18 runs, 3.20$)" in text
    assert text.index("lift:") < text.index("## Quand")
    # Idempotent : ré-écriture remplace, pas d'accumulation.
    write_lift_frontmatter(md, report)
    assert md.read_text(encoding="utf-8").count("lift:") == 1


# --------------------------------------------------------------------------
# CLI : seuil MIN_CASES et --allow-underpowered (mode debug)


def _cli_case(repo: Path) -> ReplayCase:
    return ReplayCase(
        case_id="cas00001", session_id="cas00001-full", prompt="corrige pay",
        repo=repo, base_commit="HEAD~1", head_end="deadbee", signature="sig",
    )


def _invoke_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cases: list[ReplayCase],
    *extra_args: str,
) -> tuple[object, Path, list[object]]:
    """Invoque `ghost validate` avec la sélection de cas et le runner mockés —
    on teste le SEUIL et le marquage, pas le replay lui-même."""
    from typer.testing import CliRunner

    import ghost.resolve as resolve_mod
    import ghost.validate as validate_mod
    from ghost.cli import app
    from ghost.resolve import Resolved
    from ghost.validate import SkillInfo, ValidationReport

    md = tmp_path / "SKILL.md"
    md.write_text(
        "---\nname: sk\ndescription: t.\n---\n\n## Quand utiliser\nY.\n",
        encoding="utf-8",
    )
    ran: list[object] = []

    def fake_run_validation(
        _conn: object, skill: SkillInfo, run_cases: list[ReplayCase], **_kw: object
    ) -> ValidationReport:
        ran.append(run_cases)
        return ValidationReport(skill_id=skill.skill_id, slug=skill.slug)

    monkeypatch.setattr(resolve_mod, "resolve_skill", lambda _c, _t: Resolved(id=1))
    monkeypatch.setattr(
        validate_mod, "skill_info",
        lambda _c, _i: SkillInfo(skill_id=1, slug="sk", source=md, candidate_id=1),
    )
    monkeypatch.setattr(
        validate_mod, "eligible_cases",
        lambda _c, _s, match: (cases, {"sessions_examinees": 5}),
    )
    monkeypatch.setattr(validate_mod, "run_validation", fake_run_validation)
    result = CliRunner().invoke(
        app, ["validate", "1", "--yes", "--db", str(tmp_path / "g.db"), *extra_args]
    )
    return result, md, ran


def test_validate_refuses_below_min_cases_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _md, ran = _invoke_validate(tmp_path, monkeypatch, [_cli_case(tmp_path)])
    assert result.exit_code == 1
    assert "refus" in result.output
    assert "--allow-underpowered" in result.output  # la porte de sortie est indiquée
    assert ran == []  # rien n'a tourné


def test_validate_allow_underpowered_zero_cases_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _md, ran = _invoke_validate(
        tmp_path, monkeypatch, [], "--allow-underpowered"
    )
    assert result.exit_code == 1
    assert "0 cas" in result.output
    assert ran == []


def test_validate_allow_underpowered_runs_marks_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, md, ran = _invoke_validate(
        tmp_path, monkeypatch, [_cli_case(tmp_path)], "--allow-underpowered"
    )
    assert result.exit_code == 0, result.output
    assert len(ran) == 1  # la mécanique a bien tourné (1 cas)
    assert "NON STATISTIQUEMENT VALIDE" in result.output
    # Rien d'écrit dans le SKILL.md : pas de ligne lift dans le frontmatter.
    assert "lift:" not in md.read_text(encoding="utf-8")


def test_validate_at_threshold_still_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = [_cli_case(tmp_path) for _ in range(3)]
    result, md, ran = _invoke_validate(tmp_path, monkeypatch, cases)
    assert result.exit_code == 0, result.output
    assert len(ran) == 1
    assert "NON STATISTIQUEMENT VALIDE" not in result.output
    # Le chemin nominal écrit toujours le lift dans le frontmatter.
    assert "lift:" in md.read_text(encoding="utf-8")
