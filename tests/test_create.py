"""`ghost create` : import GitHub → normalisation → skill LOCAL traité comme un
distillé maison (visible dans ghost skills, déployable, publiable/retrievable)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghost.create import (
    CreateError,
    NormalizedSkill,
    create_local_skill,
    normalize_skill,
    render_skill_md,
    resolve_github_raw,
    source_repo_from_url,
)
from ghost.db import connect
from ghost.deploy import plan_deploy
from ghost.signature import dominant_task_signature


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


def _norm(**kw: object) -> NormalizedSkill:
    base: dict[str, object] = {
        "verdict": "SKILL",
        "skip_reason": "",
        "name": "My Cool Skill",
        "description": "Use when a detached HEAD appears and you must not lose commits",
        "tags": ["Git", "Recovery"],
        "stack": ["bash", "git"],
        "signature": "bash-git-checkout|sans-fichier|head-detached|commit",
        "tokens_in": 700,
        "tokens_out": 90,
        "cost_usd": 0.0035,
    }
    base.update(kw)
    return NormalizedSkill(**base)  # type: ignore[arg-type]


# ── SSRF / résolution (port du backend) ───────────────────────────────────────
def test_resolve_github_raw_and_ssrf() -> None:
    assert (
        resolve_github_raw("https://github.com/o/r/blob/main/skills/foo/SKILL.md")
        == "https://raw.githubusercontent.com/o/r/main/skills/foo/SKILL.md"
    )
    raw = "https://raw.githubusercontent.com/o/r/main/x.md"
    assert resolve_github_raw(raw) == raw
    for bad in (
        "http://github.com/o/r/blob/main/x.md",  # pas https
        "https://evil.com/x.md",  # hôte non autorisé
        "https://raw.githubusercontent.com.evil.com/x.md",  # suffixe piège
        "https://github.com/o/r/tree/main/x",  # pas une page /blob/
    ):
        with pytest.raises(CreateError):
            resolve_github_raw(bad)


def test_source_repo_from_url() -> None:
    assert (
        source_repo_from_url("https://github.com/obra/superpowers/blob/main/s/SKILL.md")
        == "https://github.com/obra/superpowers"
    )
    assert (
        source_repo_from_url("https://raw.githubusercontent.com/obra/superpowers/main/s.md")
        == "https://github.com/obra/superpowers"
    )
    assert source_repo_from_url("https://example.com/") == ""


# ── Normalisation (caller mocké — pas d'appel LLM) ────────────────────────────
def test_normalize_skill_skill_and_skip() -> None:
    def skill_caller(
        system: str, user: str, schema: dict[str, object], max_tokens: int
    ) -> tuple[dict[str, object], int, int]:
        assert "brut d'un skill" in user
        return (
            {
                "verdict": "SKILL", "skip_reason": "", "name": "foo-bar",
                "description": "Use when X happens", "tags": ["a", "b"],
                "stack": ["python"], "signature": "edit|py|err|commit",
            },
            800, 120,
        )

    norm = normalize_skill("# some skill\ndo the thing", caller=skill_caller)
    assert norm.verdict == "SKILL" and norm.name == "foo-bar"
    assert norm.tags == ["a", "b"] and norm.stack == ["python"]
    assert norm.cost_usd > 0

    def skip_caller(
        system: str, user: str, schema: dict[str, object], max_tokens: int
    ) -> tuple[dict[str, object], int, int]:
        return ({"verdict": "SKIP", "skip_reason": "README marketing", "name": "",
                 "description": "", "tags": [], "stack": [], "signature": ""}, 500, 30)

    skip = normalize_skill("# buy now", caller=skip_caller)
    assert skip.verdict == "SKIP" and skip.skip_reason == "README marketing"


# ── Rendu : format distillé (listes) + provenance, corps préservé ─────────────
def test_render_matches_distilled_format_with_provenance() -> None:
    raw = "---\nname: old_tool_meta\n---\n# Recover\nRun the rescue.\n"
    md = render_skill_md(_norm(), source="https://github.com/o/r", license="MIT", raw_md=raw)
    assert md.startswith("---\nname: my-cool-skill\n")
    assert "tags: [git, recovery]" in md  # minuscules, kebab
    assert "stack: [bash, git]" in md  # LISTE, comme les distillés
    assert "source: https://github.com/o/r" in md
    assert "license: MIT" in md
    assert "# Recover\nRun the rescue." in md  # corps d'origine conservé
    assert "old_tool_meta" not in md  # ancien frontmatter retiré


def test_render_one_line_description_and_unknown_license() -> None:
    md = render_skill_md(
        _norm(description="Use when it fails with error: boom happens"),
        source="https://github.com/o/r", license="", raw_md="# b\ntext",
    )
    assert "license: unknown" in md  # jamais inventée
    # deux-points neutralisé (le format distillé n'échappe pas le YAML)
    assert "error — boom happens" in md
    assert "error: boom" not in md


# ── Persistance : traité EXACTEMENT comme un distillé ─────────────────────────
def test_create_local_skill_behaves_like_distilled(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    url = "https://github.com/o/r/blob/main/SKILL.md"
    norm = _norm()
    md = render_skill_md(norm, source="https://github.com/o/r", license="MIT", raw_md="# b\nx")
    created = create_local_skill(db, url=url, norm=norm, skill_md=md, skills_dir=tmp_path)

    # candidat github_import, KEPT, task_signature stockée
    cand = db.execute(
        "SELECT kind, signature, status, task_signature, session_ids_json "
        "FROM candidates WHERE id = ?",
        (created.candidate_id,),
    ).fetchone()
    assert cand == ("github_import", url, "kept", norm.signature, "[]")

    # skill SKILL actif + fichier écrit
    sk = db.execute(
        "SELECT verdict, disabled, slug FROM skills WHERE id = ?", (created.skill_id,)
    ).fetchone()
    assert sk[0] == "SKILL" and sk[1] == 0 and sk[2] == created.slug
    assert created.path.exists() and created.path.read_text(encoding="utf-8") == md

    # déployable (candidat kept) — même requête que `ghost deploy`
    actions = plan_deploy(db)
    assert any(a.slug == created.slug for a in actions)

    # publiable/retrievable : dominant_task_signature = la signature générée
    assert dominant_task_signature(db, created.candidate_id) == norm.signature


def test_reimport_versions_and_disables_old(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    url = "https://github.com/o/r/blob/main/SKILL.md"
    n1 = _norm(signature="edit|py|old|commit")
    md1 = render_skill_md(n1, source="https://github.com/o/r", license="MIT", raw_md="# a\n1")
    c1 = create_local_skill(db, url=url, norm=n1, skill_md=md1, skills_dir=tmp_path)
    assert c1.reimport is False

    n2 = _norm(signature="edit|py|new|commit")
    md2 = render_skill_md(n2, source="https://github.com/o/r", license="MIT", raw_md="# b\n2")
    c2 = create_local_skill(db, url=url, norm=n2, skill_md=md2, skills_dir=tmp_path)
    assert c2.reimport is True
    assert c2.candidate_id == c1.candidate_id  # même identité (par URL)

    # ancien désactivé, un seul skill actif, signature mise à jour
    active = db.execute(
        "SELECT id FROM skills WHERE candidate_id = ? AND verdict = 'SKILL' AND disabled = 0",
        (c1.candidate_id,),
    ).fetchall()
    assert [r[0] for r in active] == [c2.skill_id]
    assert dominant_task_signature(db, c1.candidate_id) == "edit|py|new|commit"


# ── Commande complète (CliRunner, fetch+normalize+license mockés) ─────────────
def test_create_command_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import ghost.create as create_mod
    import ghost.distill as distill_mod
    import ghost.onboard as onboard_mod
    from ghost.cli import app

    raw = "# Detached HEAD rescue\nRun git checkout -b rescue.\n"
    monkeypatch.setattr(create_mod, "fetch_skill_md", lambda _u: raw)
    monkeypatch.setattr(create_mod, "github_license_for_url", lambda _u: "MIT")
    monkeypatch.setattr(create_mod, "normalize_skill", lambda _r, *, caller: _norm())
    monkeypatch.setattr(distill_mod, "DEFAULT_SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", tmp_path / "nokey")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")

    db_path = tmp_path / "ghost.db"
    url = "https://github.com/o/r/blob/main/SKILL.md"
    result = CliRunner().invoke(app, ["create", url, "--yes", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "my-cool-skill" in result.output

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT sk.verdict, c.status, c.kind FROM skills sk "
            "JOIN candidates c ON c.id = sk.candidate_id WHERE sk.slug = 'my-cool-skill'"
        ).fetchone()
        assert row == ("SKILL", "kept", "github_import")
        assert any(a.slug == "my-cool-skill" for a in plan_deploy(conn))
    finally:
        conn.close()
    assert (tmp_path / "skills" / "my-cool-skill" / "SKILL.md").exists()


def test_create_command_skips_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import ghost.create as create_mod
    import ghost.distill as distill_mod
    import ghost.onboard as onboard_mod
    from ghost.cli import app

    monkeypatch.setattr(create_mod, "fetch_skill_md", lambda _u: "# Buy now\nmarketing")
    monkeypatch.setattr(create_mod, "github_license_for_url", lambda _u: "")
    monkeypatch.setattr(
        create_mod,
        "normalize_skill",
        lambda _r, *, caller: _norm(verdict="SKIP", skip_reason="README marketing"),
    )
    monkeypatch.setattr(distill_mod, "DEFAULT_SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", tmp_path / "nokey")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")

    db_path = tmp_path / "ghost.db"
    result = CliRunner().invoke(
        app, ["create", "https://github.com/o/r/blob/m/README.md", "--yes", "--db", str(db_path)]
    )
    assert result.exit_code == 0
    assert "SKIP" in result.output and "README marketing" in result.output
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0
    finally:
        conn.close()


def test_create_command_bad_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import ghost.onboard as onboard_mod
    from ghost.cli import app

    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", tmp_path / "nokey")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    db_path = tmp_path / "ghost.db"
    # /tree/ n'est pas une page de fichier → CreateError → sortie propre
    result = CliRunner().invoke(
        app, ["create", "https://github.com/o/r/tree/main", "--yes", "--db", str(db_path)]
    )
    assert result.exit_code == 1
    assert "lien invalide" in result.output
