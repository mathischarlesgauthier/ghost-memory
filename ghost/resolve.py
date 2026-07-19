"""Résolution tolérante des identifiants (Lot C).

Un utilisateur a un id de skill, un id de candidat, OU un slug — et n'a pas à
savoir lequel la commande attend. `validate`/`bench`/`disable`/`enable`
raisonnent en skills ; `show`/`distill`/`keep`/`reject` en candidats. Ces
helpers acceptent les trois formes et, quand l'interprétation n'est pas triviale,
renvoient une note explicative (« 291 est un candidat → skill 16 »). Plus jamais
de « not a valid int » jeté à la figure.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class ResolveError(ValueError):
    """Identifiant introuvable ou ambigu — message actionnable en clair."""


@dataclass(slots=True)
class Resolved:
    id: int
    note: str = ""


def resolve_skill(conn: sqlite3.Connection, token: str) -> Resolved:
    """Renvoie un id de SKILL à partir d'un id de skill, d'un slug, ou d'un id
    de candidat (via son skill kept)."""
    token = token.strip()
    rows = conn.execute(
        "SELECT id FROM skills WHERE slug = ? AND verdict = 'SKILL' ORDER BY id DESC",
        (token,),
    ).fetchall()
    if rows:
        if len(rows) > 1:
            ids = ", ".join(str(r[0]) for r in rows)
            return Resolved(
                int(rows[0][0]),
                note=f"plusieurs skills pour « {token} » ({ids}) — pris {rows[0][0]}",
            )
        return Resolved(int(rows[0][0]))
    if token.isdigit():
        n = int(token)
        if conn.execute(
            "SELECT 1 FROM skills WHERE id = ? AND verdict = 'SKILL'", (n,)
        ).fetchone():
            return Resolved(n)
        srows = conn.execute(
            "SELECT id, slug FROM skills WHERE candidate_id = ? AND verdict = 'SKILL' "
            "ORDER BY id DESC",
            (n,),
        ).fetchall()
        if len(srows) == 1:
            return Resolved(
                int(srows[0][0]),
                note=f"{n} est un candidat → skill {srows[0][0]} ({srows[0][1]})",
            )
        if len(srows) > 1:
            listing = ", ".join(f"{r[0]}:{r[1]}" for r in srows)
            raise ResolveError(
                f"{n} est un candidat avec plusieurs skills ({listing}) — "
                "donne l'id du skill voulu."
            )
        raise ResolveError(
            f"« {token} » n'est ni un skill, ni un candidat distillé. "
            "`ghost skills` liste les skills."
        )
    raise ResolveError(
        f"« {token} » inconnu — donne un id de skill, un slug, ou un id de candidat "
        "(`ghost skills`)."
    )


def resolve_candidate(conn: sqlite3.Connection, token: str) -> Resolved:
    """Renvoie un id de CANDIDAT à partir d'un id de candidat, d'un id de skill,
    ou d'un slug (via le candidat du skill)."""
    token = token.strip()
    if token.isdigit():
        n = int(token)
        if conn.execute("SELECT 1 FROM candidates WHERE id = ?", (n,)).fetchone():
            return Resolved(n)
        row = conn.execute(
            "SELECT candidate_id, slug FROM skills WHERE id = ?", (n,)
        ).fetchone()
        if row:
            return Resolved(
                int(row[0]), note=f"{n} est un skill ({row[1]}) → candidat {row[0]}"
            )
        raise ResolveError(
            f"candidat {n} introuvable. `ghost scan` liste les candidats."
        )
    row = conn.execute(
        "SELECT candidate_id FROM skills WHERE slug = ? ORDER BY id DESC", (token,)
    ).fetchone()
    if row:
        return Resolved(int(row[0]), note=f"slug « {token} » → candidat {row[0]}")
    raise ResolveError(
        f"« {token} » inconnu — donne un id de candidat, un id de skill, ou un slug."
    )


def skills_for_candidate(
    conn: sqlite3.Connection, candidate_id: int
) -> list[tuple[int, str]]:
    """Skills SKILL actifs (non désactivés) d'un candidat — pour la dédup."""
    return [
        (int(r[0]), str(r[1] or "—"))
        for r in conn.execute(
            "SELECT id, slug FROM skills WHERE candidate_id = ? AND verdict = 'SKILL' "
            "AND disabled = 0 ORDER BY id",
            (candidate_id,),
        )
    ]
