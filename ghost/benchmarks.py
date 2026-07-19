"""Micro-benchmarks SYNTHÉTIQUES — assumés comme tels, PAS du vrai replay.

Lot A a établi que le corpus n'a aucune session courte, auto-contenue et
rejouable : les cas réels sont des méga-missions (réseau, MCP, prod, 5-34
tours). Un lift causal ne peut donc pas se mesurer par replay de l'historique
sur ce corpus (baseline ~5 % de succès → on mesure du bruit).

Ces micro-benchmarks sont l'alternative retenue : de mini-tâches de code
auto-contenues — aucun réseau, aucun MCP, aucun secret — chacune avec un
CHECKER déterministe (un script qui sort 0 ssi la tâche est résolue). Ils
donnent une baseline SANS skill qui réussit >50 %, condition nécessaire pour
qu'un lift veuille dire quelque chose.

Ce ne sont PAS des reproductions de l'historique : tout affichage doit les
étiqueter « synthétique ». Le grader officiel est rejoué depuis l'EXTÉRIEUR du
worktree (l'agent ne peut pas le falsifier même s'il édite le `check.py` visible).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from ghost.replay import PER_RUN_BUDGET_USD, RunMetrics, inject_skill, run_replay
from ghost.validate import (
    LiftReport,
    RunRecord,
    SkillInfo,
    _existing_runs,
    _persist_run,
    aggregate_records,
)

BENCH_PREFIX = "bench:"


@dataclass(frozen=True, slots=True)
class MicroBench:
    """Une mini-tâche synthétique auto-contenue + son grader déterministe."""

    slug: str
    target_skills: tuple[str, ...]
    summary: str
    prompt: str
    stub_files: dict[str, str] = field(default_factory=dict)
    grader: str = ""  # script python : exit 0 ssi la tâche est résolue
    synthetic: bool = True

    def worktree_files(self) -> dict[str, str]:
        # `check.py` visible : l'agent peut s'auto-vérifier (`python check.py`).
        # Le grader OFFICIEL est une copie rejouée hors du worktree — non
        # falsifiable même si l'agent réécrit ce check.py.
        return {**self.stub_files, "check.py": self.grader}


# --------------------------------------------------------------------------
# Sandbox + grader


def _git(work: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(work), *args], capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[:160]}")


@contextmanager
def bench_sandbox(bench: MicroBench) -> Iterator[Path]:
    """Crée un mini-repo git frais pour le banc, puis nettoie. Aucun remote,
    aucun réseau — l'état initial est entièrement défini par le banc."""
    tmp = Path(tempfile.mkdtemp(prefix="ghost-bench-"))
    try:
        work = tmp / "repo"
        work.mkdir()
        for rel, content in bench.worktree_files().items():
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _git(work, "init", "-q")
        _git(work, "config", "user.email", "bench@ghost")
        _git(work, "config", "user.name", "ghost-bench")
        _git(work, "add", ".")
        _git(work, "commit", "-qm", "bench: etat initial")
        yield work
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def grade(bench: MicroBench, work: Path) -> bool:
    """Critère ✓/✗ : rejoue le grader canonique du banc DEPUIS L'EXTÉRIEUR
    (l'agent ne peut pas le trafiquer), avec le worktree sur le PYTHONPATH."""
    tmp = Path(tempfile.mkdtemp(prefix="ghost-bench-grade-"))
    try:
        grader = tmp / "grader.py"
        grader.write_text(bench.grader, encoding="utf-8")
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
            if k in os.environ
        }
        env["PYTHONPATH"] = str(work)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, str(grader)],
            cwd=work, env=env, capture_output=True, text=True, timeout=120,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Protocole (miroir de validate.run_validation, critère = grader du banc)

BenchRunner = Callable[..., RunMetrics]
BenchSandbox = Callable[[MicroBench], AbstractContextManager[Path]]


def benches_for(slug: str) -> list[MicroBench]:
    return [b for b in BENCHES if slug in b.target_skills]


def run_bench_validation(
    conn: sqlite3.Connection,
    skill: SkillInfo,
    benches: list[MicroBench],
    *,
    max_cost_usd: float,
    n_per_condition: int = 3,
    per_run_budget: float = PER_RUN_BUDGET_USD,
    runner: BenchRunner = run_replay,
    sandbox_factory: BenchSandbox = bench_sandbox,
    on_progress: Callable[[str], None] = lambda _msg: None,
) -> LiftReport:
    """Rejoue chaque banc avec/sans le skill, ordre alterné, budget dur,
    persistance (reprise). Le succès d'un run = le grader du banc passe."""
    from ghost.validate import ValidationReport

    report = ValidationReport(skill_id=skill.skill_id, slug=skill.slug)
    done = _existing_runs(conn, skill.skill_id)
    report.spent_usd = float(
        conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM replays WHERE skill_id = ? "
            "AND case_id LIKE ?",
            (skill.skill_id, BENCH_PREFIX + "%"),
        ).fetchone()[0]
    )
    schedule: list[tuple[MicroBench, str, int]] = []
    for bench in benches:
        for run_idx in range(n_per_condition):
            schedule.append((bench, "sans", run_idx))
            schedule.append((bench, "avec", run_idx))

    consecutive_errors = 0
    for bench, condition, run_idx in schedule:
        case_id = BENCH_PREFIX + bench.slug
        if (case_id, condition, run_idx) in done:
            continue
        if report.spent_usd + per_run_budget > max_cost_usd:
            report.stopped_on_budget = True
            on_progress(f"arret budget : {report.spent_usd:.2f}$ depenses")
            break
        if consecutive_errors >= 3:
            report.errors.append("3 echecs consecutifs — arret")
            break
        on_progress(f"bench {bench.slug} · {condition} · run {run_idx + 1}")
        try:
            with sandbox_factory(bench) as work:
                if condition == "avec":
                    inject_skill(work, skill.source, skill.slug)
                metrics = runner(
                    work, bench.prompt, budget_usd=per_run_budget,
                    checker=partial(grade, bench),
                )
        except Exception as exc:  # le run a pu dépenser avant l'échec du post-traitement
            report.errors.append(f"{bench.slug}/{condition}/{run_idx}: {exc}")
            report.spent_usd += per_run_budget
            consecutive_errors += 1
            continue
        consecutive_errors = 0
        record = RunRecord(case_id, condition, run_idx, metrics)
        _persist_run(conn, skill.skill_id, record)
        report.records.append(record)
        report.spent_usd += metrics.cost_usd

    return aggregate_bench(conn, skill.skill_id)


def aggregate_bench(conn: sqlite3.Connection, skill_id: int) -> LiftReport:
    """Agrège UNIQUEMENT les runs de banc (`case_id` préfixé), pour ne jamais
    mélanger avec d'anciens replays réels du même skill."""
    import json as _json

    rows = conn.execute(
        "SELECT case_id, condition, metrics_json, cost_usd FROM replays "
        "WHERE skill_id = ? AND case_id LIKE ?",
        (skill_id, BENCH_PREFIX + "%"),
    ).fetchall()
    records: list[tuple[str, str, dict[str, object]]] = []
    for case_id, condition, metrics_json, cost in rows:
        m = _json.loads(str(metrics_json))
        if isinstance(m, dict):
            m.setdefault("cost_usd", float(cost))
            records.append((str(case_id), str(condition), m))
    return aggregate_records(records, skill_id=skill_id)


# --------------------------------------------------------------------------
# Le catalogue de bancs (dérivés des scars réels du corpus)

BENCHES: list[MicroBench] = [
    MicroBench(
        slug="read-modify-constant",
        target_skills=("edit-file-modified-since-read", "edit-stale-read-recovery"),
        summary="Lire un module puis corriger une constante et une fonction.",
        prompt=(
            "Le fichier settings.py ne respecte pas SPEC.md. Lis SPEC.md, corrige "
            "settings.py pour que `python check.py` sorte OK, puis committe.\n"
            "Ne modifie que settings.py."
        ),
        stub_files={
            "SPEC.md": (
                "# Spec settings.py\n\n"
                "- `BASE_TIMEOUT` doit valoir 30 (actuellement faux).\n"
                "- `effective_timeout(retries)` doit renvoyer "
                "`BASE_TIMEOUT * (retries + 1)`.\n"
                "- `retry_delays(n)` doit renvoyer la liste "
                "`[BASE_TIMEOUT * 2**i for i in range(n)]`.\n\n"
                "Vérifie avec `python check.py`.\n"
            ),
            "settings.py": (
                "BASE_TIMEOUT = 5\n\n\n"
                "def effective_timeout(retries):\n"
                "    return BASE_TIMEOUT * (retries + 1)\n\n\n"
                "def retry_delays(n):\n"
                "    return []\n"
            ),
        },
        grader=(
            "import settings\n"
            "assert settings.BASE_TIMEOUT == 30, settings.BASE_TIMEOUT\n"
            "assert settings.effective_timeout(0) == 30\n"
            "assert settings.effective_timeout(2) == 90\n"
            "assert settings.retry_delays(3) == [30, 60, 120], settings.retry_delays(3)\n"
            "print('OK')\n"
        ),
    ),
    MicroBench(
        slug="status-code-contract",
        target_skills=("api-tester-status-code-guessing",),
        summary="Faire correspondre des codes de statut HTTP à un contrat écrit.",
        prompt=(
            "handler.py ne respecte pas le contrat de CONTRACT.md. Lis le contrat, "
            "corrige `status_for(kind)` dans handler.py pour que `python check.py` "
            "sorte OK, puis committe. Ne modifie que handler.py."
        ),
        stub_files={
            "CONTRACT.md": (
                "# Contrat status_for(kind)\n\n"
                "| kind        | code |\n"
                "|-------------|------|\n"
                "| 'created'   | 201  |\n"
                "| 'ok'        | 200  |\n"
                "| 'missing'   | 404  |\n"
                "| 'invalid'   | 422  |\n"
                "| 'conflict'  | 409  |\n"
                "| autre       | 500  |\n"
            ),
            "handler.py": (
                "def status_for(kind):\n"
                "    # À corriger : devine des codes au lieu de suivre le contrat.\n"
                "    if kind == 'ok':\n"
                "        return 200\n"
                "    return 400\n"
            ),
        },
        grader=(
            "import handler\n"
            "expected = {'created': 201, 'ok': 200, 'missing': 404,\n"
            "            'invalid': 422, 'conflict': 409, 'whatever': 500}\n"
            "for kind, code in expected.items():\n"
            "    got = handler.status_for(kind)\n"
            "    assert got == code, (kind, got, code)\n"
            "print('OK')\n"
        ),
    ),
    MicroBench(
        slug="schema-shaped-output",
        target_skills=(
            "structured-output-schema-retry",
            "structuredoutput-findings-missing-loop",
        ),
        summary="Produire un dict conforme à un schéma décrit.",
        prompt=(
            "build.py doit renvoyer un enregistrement conforme à SCHEMA.md. Lis le "
            "schéma, implémente `build_record(name, tags)` pour que `python check.py` "
            "sorte OK, puis committe. Ne modifie que build.py."
        ),
        stub_files={
            "SCHEMA.md": (
                "# Schéma de build_record(name, tags)\n\n"
                "Renvoie un dict avec EXACTEMENT ces clés :\n"
                "- `name` : str, la valeur passée.\n"
                "- `slug` : str, `name` en minuscules, espaces -> '-'.\n"
                "- `tags` : list[str], triée, sans doublon.\n"
                "- `tag_count` : int, longueur de `tags`.\n"
                "- `version` : int, toujours 1.\n"
            ),
            "build.py": (
                "def build_record(name, tags):\n"
                "    return {'name': name}\n"
            ),
        },
        grader=(
            "from build import build_record\n"
            "r = build_record('Hello World', ['b', 'a', 'b'])\n"
            "assert set(r) == {'name', 'slug', 'tags', 'tag_count', 'version'}, set(r)\n"
            "assert r['name'] == 'Hello World'\n"
            "assert r['slug'] == 'hello-world', r['slug']\n"
            "assert r['tags'] == ['a', 'b'], r['tags']\n"
            "assert r['tag_count'] == 2\n"
            "assert r['version'] == 1\n"
            "print('OK')\n"
        ),
    ),
]
