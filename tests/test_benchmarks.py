"""Lot B : micro-benchmarks synthétiques + garde-fous de mesure.

Ces tests prouvent les propriétés EXIGÉES sans dépenser d'API (runner mocké) :
- baseline SANS skill > 50 % de succès (le harnais sait exprimer une baseline
  qui marche) ;
- runs coupés (budget/timeout) = catégorie distincte, JAMAIS des ✗ ;
- garde-fou anti-triche : un skill nul (aucun effet) donne un lift ~0 ;
- un skill réellement utile (moins de tours partout) produit un lift.
Le grader réel des bancs est testé pour de vrai (rejette le stub, accepte une
solution correcte)."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ghost.benchmarks import (
    BENCHES,
    MicroBench,
    aggregate_bench,
    bench_sandbox,
    grade,
    run_bench_validation,
)
from ghost.db import connect
from ghost.replay import RunMetrics
from ghost.validate import skill_info
from tests.test_detect import _SEQ
from tests.test_validate import _seed_skill_for


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    _SEQ.clear()
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


# --------------------------------------------------------------------------
# Le grader réel : rejette le stub, accepte une solution correcte

_SOLUTIONS: dict[str, tuple[str, str]] = {
    "read-modify-constant": (
        "settings.py",
        "BASE_TIMEOUT = 30\n\n\n"
        "def effective_timeout(retries):\n"
        "    return BASE_TIMEOUT * (retries + 1)\n\n\n"
        "def retry_delays(n):\n"
        "    return [BASE_TIMEOUT * 2 ** i for i in range(n)]\n",
    ),
    "status-code-contract": (
        "handler.py",
        "def status_for(kind):\n"
        "    return {'created': 201, 'ok': 200, 'missing': 404,\n"
        "            'invalid': 422, 'conflict': 409}.get(kind, 500)\n",
    ),
    "schema-shaped-output": (
        "build.py",
        "def build_record(name, tags):\n"
        "    t = sorted(set(tags))\n"
        "    return {'name': name, 'slug': name.lower().replace(' ', '-'),\n"
        "            'tags': t, 'tag_count': len(t), 'version': 1}\n",
    ),
}


@pytest.mark.parametrize("bench", BENCHES, ids=lambda b: b.slug)
def test_grader_rejects_stub_accepts_solution(bench: MicroBench) -> None:
    with bench_sandbox(bench) as work:
        # L'état initial (le stub) ne résout PAS la tâche → grader honnête.
        assert grade(bench, work) is False
        # Une solution correcte passe le grader.
        filename, content = _SOLUTIONS[bench.slug]
        (work / filename).write_text(content, encoding="utf-8")
        assert grade(bench, work) is True


def test_grader_ignores_tampered_visible_check(tmp_path: Path) -> None:
    """Même si l'agent réécrit le check.py visible pour qu'il passe, le grader
    officiel (rejoué hors worktree) juge le vrai module."""
    bench = next(b for b in BENCHES if b.slug == "status-code-contract")
    with bench_sandbox(bench) as work:
        (work / "check.py").write_text("print('OK')\n", encoding="utf-8")
        assert grade(bench, work) is False  # handler.py toujours faux


# --------------------------------------------------------------------------
# Protocole mocké : propriétés de mesure

_B1 = MicroBench(
    slug="b1", target_skills=("sk",), summary="", prompt="p",
    grader="import sys; sys.exit(0)",
)
_B2 = MicroBench(
    slug="b2", target_skills=("sk",), summary="", prompt="p",
    grader="import sys; sys.exit(0)",
)


@contextmanager
def _fresh_sandbox(_bench: MicroBench) -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="fake-bench-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _mk_metrics(turns: int, success: bool, **kw: object) -> RunMetrics:
    return RunMetrics(
        turns=turns, cost_usd=0.10, duration_ms=1000, output_tokens=turns * 100,
        tool_errors=0, new_commits=1 if success else 0, changed_lines=5,
        success=success, is_error=False, denials=0, **kw,  # type: ignore[arg-type]
    )


def _condition_is_avec(work: Path, slug: str) -> bool:
    return (work / ".claude" / "skills" / slug / "SKILL.md").exists()


def _run(
    db: sqlite3.Connection, runner: object, *, benches: list[MicroBench] | None = None
) -> object:
    skill_id = _seed_skill_for(db, [])
    skill = skill_info(db, skill_id)
    return run_bench_validation(
        db, skill, benches or [_B1, _B2],
        max_cost_usd=100.0, n_per_condition=3,
        runner=runner,  # type: ignore[arg-type]
        sandbox_factory=_fresh_sandbox,
    )


def test_baseline_over_50pct(db: sqlite3.Connection) -> None:
    """Le harnais sait exprimer une baseline SANS skill qui réussit >50 %."""
    skill_slug = "sk"

    def runner(work: Path, _prompt: str, **_kw: object) -> RunMetrics:
        # sans : réussit ; avec : réussit aussi. Baseline franchement >50 %.
        _ = _condition_is_avec(work, skill_slug)
        return _mk_metrics(turns=8, success=True)

    lift = _run(db, runner)
    sans_ok, sans_n = lift.success_sans  # type: ignore[attr-defined]
    assert sans_n > 0 and sans_ok / sans_n > 0.5


def test_budget_cut_never_counts_as_failure(db: sqlite3.Connection) -> None:
    def runner(work: Path, _prompt: str, **_kw: object) -> RunMetrics:
        # Tout run est coupé par le budget → 0 succès mais 0 ✗ non plus.
        return _mk_metrics(turns=3, success=False, budget_exhausted=True)

    lift = _run(db, runner)
    # Aucun run complet → dénominateur de succès à 0, tout en incomplets.
    assert lift.success_sans[1] == 0 and lift.success_avec[1] == 0  # type: ignore[attr-defined]
    assert lift.incomplete_sans > 0 and lift.incomplete_avec > 0  # type: ignore[attr-defined]
    assert lift.verdict == "pas de lift mesurable"  # type: ignore[attr-defined]


def test_null_skill_gives_no_lift(db: sqlite3.Connection) -> None:
    """Garde-fou anti-triche : un skill sans effet ne doit JAMAIS produire de
    lift. Le runner varie (bruit) mais INDÉPENDAMMENT de la condition — donc
    avec et sans ont la même distribution, et la mesure doit dire « pas de lift »."""
    seq = iter([10, 11, 12, 10, 11, 12, 11, 12, 10, 11, 12, 10])

    def runner(_work: Path, _prompt: str, **_kw: object) -> RunMetrics:
        return _mk_metrics(turns=next(seq), success=True)

    lift = _run(db, runner)
    assert lift.verdict == "pas de lift mesurable"  # type: ignore[attr-defined]


def test_useful_skill_shows_lift(db: sqlite3.Connection) -> None:
    """Un skill réellement utile (moins de tours partout) produit un lift."""
    skill_slug = "sk"

    def runner(work: Path, _prompt: str, **_kw: object) -> RunMetrics:
        avec = _condition_is_avec(work, skill_slug)
        return _mk_metrics(turns=5 if avec else 12, success=True)

    lift = _run(db, runner)
    assert "lift" in lift.verdict and "turns" in lift.verdict  # type: ignore[attr-defined]
    assert lift.success_sans == (6, 6) and lift.success_avec == (6, 6)  # type: ignore[attr-defined]


def test_resume_skips_done_runs(db: sqlite3.Connection) -> None:
    calls = {"n": 0}

    def runner(_work: Path, _prompt: str, **_kw: object) -> RunMetrics:
        calls["n"] += 1
        return _mk_metrics(turns=8, success=True)

    skill_id = _seed_skill_for(db, [])
    skill = skill_info(db, skill_id)
    run_bench_validation(
        db, skill, [_B1], max_cost_usd=100.0, n_per_condition=3,
        runner=runner, sandbox_factory=_fresh_sandbox,  # type: ignore[arg-type]
    )
    first = calls["n"]
    assert first == 6  # 1 banc, 2 conditions, 3 runs
    # Relance : rien à refaire.
    lift = aggregate_bench(db, skill_id)
    run_bench_validation(
        db, skill, [_B1], max_cost_usd=100.0, n_per_condition=3,
        runner=runner, sandbox_factory=_fresh_sandbox,  # type: ignore[arg-type]
    )
    assert calls["n"] == first  # aucun run supplémentaire
    assert lift.n_runs == 6
