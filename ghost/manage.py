"""Transparence et kill switch — le mode d'échec n°1 est l'injection
silencieuse qui dégrade l'agent sans retour possible.

Ghost Memory n'installe AUCUN hook dans settings.json : `ghost deploy` place
des SKILL.md que Claude Code découvre nativement dans ~/.claude/skills/ et
<projet>/.claude/skills/. Désactiver = retirer le fichier déployé (Claude Code
cesse de le charger) et marquer le skill pour que `ghost deploy` ne le
repousse pas. Désinstaller = retirer tous les fichiers déployés.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SKILL_LINE_RE = re.compile(r"^- ([\w-]+): (.*)$", re.M)


@dataclass(slots=True)
class InjectedSkill:
    slug: str
    description: str
    skill_id: int | None


def last_session_id(conn: sqlite3.Connection) -> str | None:
    """Session à l'activité la plus RÉCENTE (max ts d'event), pas la plus
    récemment démarrée — une longue session reprise ne doit pas perdre face
    à une session courte plus récente."""
    row = conn.execute(
        "SELECT session_id FROM events WHERE agent_id IS NULL AND ts IS NOT NULL "
        "GROUP BY session_id ORDER BY MAX(ts) DESC LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else None


def _is_safe_skill_path(path: Path) -> bool:
    """Un chemin déployé est sûr à supprimer ssi c'est un SKILL.md sous un
    répertoire `.claude/skills/<slug>/` et pas un lien symbolique."""
    if path.name != "SKILL.md" or path.is_symlink():
        return False
    parts = path.parts
    try:
        idx = parts.index("skills")
    except ValueError:
        return False
    # …/.claude/skills/<slug>/SKILL.md → 'skills' à l'avant-dernier-1.
    return idx >= 1 and parts[idx - 1] == ".claude" and idx == len(parts) - 3


def why_last(conn: sqlite3.Connection) -> tuple[str | None, list[InjectedSkill]]:
    """Skills Ghost déployés qui étaient DISPONIBLES (donc injectables) dans
    la dernière session — avec la description qui a servi de déclencheur."""
    session_id = last_session_id(conn)
    if session_id is None:
        return None, []
    deployed = {
        str(slug): int(sid)
        for slug, sid in conn.execute(
            "SELECT sk.slug, sk.id FROM deployments d JOIN skills sk ON sk.id = d.skill_id "
            "WHERE sk.slug IS NOT NULL AND sk.disabled = 0"
        )
    }
    listed: dict[str, str] = {}
    for (text,) in conn.execute(
        "SELECT text FROM events WHERE session_id = ? AND agent_id IS NULL "
        "AND block_type = 'skill_listing'",
        (session_id,),
    ):
        for slug, desc in _SKILL_LINE_RE.findall(str(text or "")):
            listed[slug] = desc.strip()
    injected = [
        InjectedSkill(slug=slug, description=listed[slug], skill_id=deployed.get(slug))
        for slug in listed
        if slug in deployed
    ]
    return session_id, injected


def _deployed_paths(
    conn: sqlite3.Connection, skill_id: int | None = None
) -> list[tuple[int, Path]]:
    if skill_id is None:
        rows = conn.execute("SELECT skill_id, target_path FROM deployments").fetchall()
    else:
        rows = conn.execute(
            "SELECT skill_id, target_path FROM deployments WHERE skill_id = ?",
            (skill_id,),
        ).fetchall()
    return [(int(sid), Path(str(p))) for sid, p in rows]


def _remove_deployed(paths: list[tuple[int, Path]]) -> tuple[list[Path], list[Path]]:
    """Supprime les SKILL.md confinés ; retourne (retirés, refusés). Un
    chemin hors `.claude/skills/<slug>/SKILL.md` ou symlink n'est JAMAIS
    touché — le rmdir ne s'applique qu'au répertoire de slug devenu vide."""
    removed: list[Path] = []
    refused: list[Path] = []
    for _sid, path in paths:
        if not _is_safe_skill_path(path):
            refused.append(path)
            continue
        if path.exists():
            path.unlink()
            removed.append(path)
        parent = path.parent  # …/skills/<slug>/
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    return removed, refused


def disable_skill(conn: sqlite3.Connection, skill_id: int) -> tuple[list[Path], list[Path]]:
    """Retire les SKILL.md déployés de ce skill et le marque disabled.
    Retourne (retirés, refusés-hors-confinement)."""
    removed, refused = _remove_deployed(_deployed_paths(conn, skill_id))
    conn.execute("UPDATE skills SET disabled = 1 WHERE id = ?", (skill_id,))
    conn.execute("DELETE FROM deployments WHERE skill_id = ?", (skill_id,))
    conn.commit()
    return removed, refused


def enable_skill(conn: sqlite3.Connection, skill_id: int) -> None:
    """Réactive un skill désactivé (il redevient déployable via ghost deploy)."""
    conn.execute("UPDATE skills SET disabled = 0 WHERE id = ?", (skill_id,))
    conn.commit()


def uninstall_skills(conn: sqlite3.Connection) -> tuple[list[Path], list[Path]]:
    """Retire TOUS les SKILL.md déployés par Ghost Memory (aucun hook à
    retirer : Ghost Memory n'en installe pas). Ne marque disabled QUE les
    skills qui avaient un déploiement (pas ceux jamais déployés)."""
    paths = _deployed_paths(conn)
    removed, refused = _remove_deployed(paths)
    skill_ids = {sid for sid, _ in paths}
    if skill_ids:
        placeholders = ",".join("?" * len(skill_ids))
        conn.execute(
            f"UPDATE skills SET disabled = 1 WHERE id IN ({placeholders})",
            list(skill_ids),
        )
    conn.execute("DELETE FROM deployments")
    conn.commit()
    return removed, refused
