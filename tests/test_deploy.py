"""Tests lot 4 : déploiement (split global/projet), triage, pipeline plafonné."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghost.db import connect
from ghost.deploy import apply_deploy, convert_for_claude_code, plan_deploy
from ghost.pipeline import run_pipeline
from tests.test_detect import _SEQ, tool_call

SKILL_MD = """---
name: test-skill
description: Quand tu testes le déploiement.
tags: [a, b]
stack: [python]
---

## Quand utiliser
Toujours.

## Pièges
- **X.**
  - Preuve : [1] erreur.
"""


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    _SEQ.clear()
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


def _seed_skill(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    candidate_id: int,
    slug: str,
    sessions: list[tuple[str, str, str]],  # (session_id, project, cwd)
    status: str = "kept",
    low_value: int = 0,
) -> Path:
    for sid, project, cwd in sessions:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, project, cwd) VALUES (?, ?, ?)",
            (sid, project, cwd),
        )
    conn.execute(
        "INSERT INTO candidates (id, kind, signature, status, session_ids_json)"
        " VALUES (?, 'FAILURE_LOOP', ?, ?, ?)",
        (candidate_id, f"sig-{slug}", status, json.dumps([s[0] for s in sessions])),
    )
    source_dir = tmp_path / "ghost-skills" / slug
    source_dir.mkdir(parents=True)
    source = source_dir / "SKILL.md"
    source.write_text(SKILL_MD.replace("test-skill", slug), encoding="utf-8")
    conn.execute(
        "INSERT INTO skills (candidate_id, slug, path, model, prompt_version, verdict,"
        " low_value) VALUES (?, ?, ?, 'm', '1', 'SKILL', ?)",
        (candidate_id, slug, str(source), low_value),
    )
    conn.commit()
    return source


def test_plan_deploy_splits_project_vs_global(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    project_dir = tmp_path / "monprojet"
    project_dir.mkdir()
    _seed_skill(db, tmp_path, candidate_id=1, slug="skill-projet",
                sessions=[("s1", "proj-a", str(project_dir))])
    _seed_skill(db, tmp_path, candidate_id=2, slug="skill-generique",
                sessions=[("s2", "proj-a", str(project_dir)),
                          ("s3", "proj-b", str(tmp_path))])
    _seed_skill(db, tmp_path, candidate_id=3, slug="skill-cwd-disparu",
                sessions=[("s4", "proj-c", str(tmp_path / "n-existe-plus"))])

    actions = {a.slug: a for a in plan_deploy(db)}
    assert actions["skill-projet"].scope == "project"
    assert actions["skill-projet"].target_dir == project_dir / ".claude" / "skills"
    assert actions["skill-generique"].scope == "global"  # multi-projets
    assert actions["skill-cwd-disparu"].scope == "global"  # cwd introuvable

    # Override explicite : générique même si mono-projet.
    forced = {
        a.slug: a for a in plan_deploy(db, force_global=frozenset({"skill-projet"}))
    }
    assert forced["skill-projet"].scope == "global"


def test_plan_deploy_filters_status_and_low_value(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_skill(db, tmp_path, candidate_id=1, slug="garde",
                sessions=[("s1", "p", str(tmp_path))], status="kept")
    _seed_skill(db, tmp_path, candidate_id=2, slug="non-triage",
                sessions=[("s2", "p", str(tmp_path))], status="distilled")
    _seed_skill(db, tmp_path, candidate_id=3, slug="rejete",
                sessions=[("s3", "p", str(tmp_path))], status="rejected")
    _seed_skill(db, tmp_path, candidate_id=4, slug="faible",
                sessions=[("s4", "p", str(tmp_path))], status="kept", low_value=1)

    slugs = {a.slug for a in plan_deploy(db)}
    assert slugs == {"garde"}
    slugs_lv = {a.slug for a in plan_deploy(db, include_low_value=True)}
    assert slugs_lv == {"garde", "faible"}


def test_apply_deploy_writes_minimal_frontmatter(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    project_dir = tmp_path / "monprojet"
    project_dir.mkdir()
    _seed_skill(db, tmp_path, candidate_id=1, slug="skill-projet",
                sessions=[("s1", "proj-a", str(project_dir))])
    actions = plan_deploy(db)
    apply_deploy(db, actions)

    deployed = project_dir / ".claude" / "skills" / "skill-projet" / "SKILL.md"
    assert deployed.exists()
    content = deployed.read_text(encoding="utf-8")
    assert "name: skill-projet" in content
    assert "description: Quand tu testes le déploiement." in content
    assert "tags:" not in content  # frontmatter réduit à name+description
    assert "## Pièges" in content
    n_dep = db.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
    assert n_dep == 1
    # Re-déploiement : upsert, pas de doublon.
    apply_deploy(db, actions)
    assert db.execute("SELECT COUNT(*) FROM deployments").fetchone()[0] == 1


def test_convert_frontmatter_without_match_passthrough() -> None:
    assert convert_for_claude_code("pas de frontmatter") == "pas de frontmatter"


def test_pipeline_budget_and_selection(db: sqlite3.Connection, tmp_path: Path) -> None:
    # 3 candidats réels via le scan (loops Bash distincts dans 3 sessions).
    # Motifs sans chiffres : la normalisation strip les nombres, des motifs
    # numérotés fusionneraient en une seule signature.
    for motif, s in [("alpha", "s1"), ("beta", "s2"), ("gamma", "s3")]:
        tool_call(db, s, "Bash", f"a{motif}", err=1, result=f"Exit code 1\nerreur {motif}")
        tool_call(db, s, "Bash", f"b{motif}", err=1, result=f"Exit code 1\nerreur {motif}")
        tool_call(db, s, "Bash", f"c{motif}", result="ok")

    def caller(
        system: str, user: str, schema: dict[str, object], max_tokens: int
    ) -> tuple[dict[str, object], int, int]:
        # coût ~0.9$ par distillation (300k tokens in) → 2 max sous 2$.
        return {"decision": "SKIP", "skip_reason": "rien"}, 300_000, 0

    report = run_pipeline(
        db, caller=caller, root=tmp_path / "vide", budget_usd=2.0, top_n=10
    )
    outcomes = [i.outcome for i in report.items]
    assert outcomes.count("SKIP") == 2  # 0.9 + 0.9 ; +0.21 dépasserait 2$
    assert outcomes.count("BUDGET") == 1
    assert report.spent_usd == pytest.approx(1.8)

    # Re-run : les candidats déjà distillés (ligne skills) ne sont pas re-sélectionnés.
    report2 = run_pipeline(
        db, caller=caller, root=tmp_path / "vide", budget_usd=2.0, top_n=10
    )
    assert [i.outcome for i in report2.items] == ["SKIP"]
