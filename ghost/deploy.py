"""Déploiement des skills validés (`ghost keep`) vers Claude Code.

Split validé : un skill dont toutes les sessions viennent d'UN projet au
cwd encore existant est déployé dans `<cwd>/.claude/skills/` (chargé par
les sessions de ce projet) ; les autres — multi-projets ou cwd disparu —
vont dans `~/.claude/skills/` (global). Mise à jour en place par slug,
jamais de suppression. Seul le frontmatter name+description est conservé
au déploiement (le format skills de Claude Code n'a besoin que de ça pour
le déclenchement).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

GLOBAL_SKILLS_DIR = Path.home() / ".claude" / "skills"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


@dataclass(slots=True)
class DeployAction:
    skill_id: int
    candidate_id: int
    slug: str
    source: Path
    target_dir: Path
    scope: str  # 'global' | 'project'
    low_value: bool


def _candidate_target(conn: sqlite3.Connection, candidate_id: int) -> tuple[Path, str]:
    row = conn.execute(
        "SELECT session_ids_json FROM candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    session_ids = json.loads(str(row[0])) if row else []
    if session_ids:
        placeholders = ",".join("?" * len(session_ids))
        rows = conn.execute(
            f"SELECT project, cwd FROM sessions WHERE id IN ({placeholders})",
            session_ids,
        ).fetchall()
        projects = {str(p) for p, _ in rows}
        cwds = [str(c) for _, c in rows if c]
        if len(projects) == 1 and cwds:
            top_cwd = Counter(cwds).most_common(1)[0][0]
            if Path(top_cwd).is_dir():
                return Path(top_cwd) / ".claude" / "skills", "project"
    return GLOBAL_SKILLS_DIR, "global"


def plan_deploy(
    conn: sqlite3.Connection,
    *,
    include_low_value: bool = False,
    force_global: frozenset[str] = frozenset(),
) -> list[DeployAction]:
    """Skills à déployer : dernier skill SKILL de chaque candidat `kept`.

    `force_global` : slugs à déployer en global même si toutes leurs
    occurrences viennent d'un seul projet (un piège d'outil harness reste
    générique même s'il n'a été payé que dans un projet)."""
    rows = conn.execute(
        """
        SELECT sk.id, sk.candidate_id, sk.slug, sk.path, sk.low_value
        FROM skills sk
        JOIN candidates c ON c.id = sk.candidate_id
        WHERE sk.verdict = 'SKILL' AND sk.path IS NOT NULL
          AND c.status = 'kept'
          AND sk.id = (SELECT MAX(s2.id) FROM skills s2
                       WHERE s2.candidate_id = sk.candidate_id AND s2.verdict = 'SKILL')
        ORDER BY sk.candidate_id
        """
    ).fetchall()
    actions: list[DeployAction] = []
    for skill_id, candidate_id, slug, path, low_value in rows:
        if low_value and not include_low_value:
            continue
        source = Path(str(path))
        if not source.exists():
            continue
        if str(slug) in force_global:
            target_dir, scope = GLOBAL_SKILLS_DIR, "global"
        else:
            target_dir, scope = _candidate_target(conn, int(candidate_id))
        actions.append(
            DeployAction(
                skill_id=int(skill_id),
                candidate_id=int(candidate_id),
                slug=str(slug),
                source=source,
                target_dir=target_dir,
                scope=scope,
                low_value=bool(low_value),
            )
        )
    return actions


def convert_for_claude_code(md_text: str) -> str:
    """Réduit le frontmatter à name + description (contrat de déclenchement
    des skills Claude Code) ; le corps est conservé tel quel."""
    match = _FRONTMATTER_RE.match(md_text)
    if not match:
        return md_text
    name = description = ""
    for line in match.group(1).splitlines():
        if line.startswith("name:"):
            name = line.removeprefix("name:").strip()
        elif line.startswith("description:"):
            description = line.removeprefix("description:").strip()
    body = md_text[match.end() :]
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def apply_deploy(conn: sqlite3.Connection, actions: list[DeployAction]) -> None:
    now = datetime.now(UTC).isoformat()
    for action in actions:
        skill_dir = action.target_dir / action.slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = convert_for_claude_code(action.source.read_text(encoding="utf-8"))
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        conn.execute(
            """
            INSERT INTO deployments (skill_id, target_path, deployed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(skill_id, target_path) DO UPDATE SET deployed_at = excluded.deployed_at
            """,
            (action.skill_id, str(skill_dir / "SKILL.md"), now),
        )
    conn.commit()
