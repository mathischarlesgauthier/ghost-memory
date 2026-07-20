"""Signature de tâche : même signature = même classe de tâche.

Composée des outils dominants (tokens enrichis), des extensions touchées,
du motif d'erreur dominant (normalisé — chemins/uuid/nombres strippés) et
de la présence d'un commit final. Volontairement grossière : trop fine,
aucune session ne s'apparie ; l'arbitrage de granularité se lit dans la
sortie de `ghost watch` (classes exclues faute de contrepartie).

Réutilisée par le lot 6 (sélection des cas de replay).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from ghost.detect import (
    GIT_COMMIT_RE,
    bash_token,
    normalize_error,
    tool_input,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, max_len: int = 28) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:max_len] or "aucun"


def task_signature(conn: sqlite3.Connection, session_id: str) -> str:
    """Signature lisible et stable d'une session, ex. :
    `bash-uv-run-pytest+edit|py|modulenotfounderror-no-module|commit`."""
    # Outils dominants (tokens enrichis pour Bash), tous threads confondus.
    tool_counts: Counter[str] = Counter()
    has_commit = False
    for tool_name, payload in conn.execute(
        "SELECT tool_name, payload_json FROM events "
        "WHERE session_id = ? AND block_type = 'tool_use' AND tool_name IS NOT NULL",
        (session_id,),
    ):
        if tool_name == "Bash":
            command = tool_input(str(payload) if payload else None).get("command")
            if isinstance(command, str):
                tool_counts[bash_token(command)] += 1
                if GIT_COMMIT_RE.search(command):
                    has_commit = True
            else:
                tool_counts["Bash"] += 1
        else:
            tool_counts[str(tool_name)] += 1
    # Tri déterministe (-count, nom) : most_common départage les égalités
    # par ordre d'insertion, deux sessions identiques divergeraient.
    top_tools = [
        _slug(t)
        for t, _ in sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
    ]

    # Extensions touchées (Edit/Write/Read, via files_touched).
    ext_counts: Counter[str] = Counter()
    for (path,) in conn.execute(
        "SELECT f.path FROM files_touched f JOIN events e ON e.id = f.event_id "
        "WHERE e.session_id = ?",
        (session_id,),
    ):
        suffix = Path(str(path)).suffix.lstrip(".")
        if suffix:
            ext_counts[suffix.lower()] += 1
    top_exts = sorted(
        ext
        for ext, _ in sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
    )

    # Motif d'erreur dominant (normalisé).
    error_counts: Counter[str] = Counter()
    for (text,) in conn.execute(
        "SELECT text FROM events WHERE session_id = ? AND block_type = 'tool_result' "
        "AND is_error = 1 AND text IS NOT NULL",
        (session_id,),
    ):
        error_counts[_slug(normalize_error(str(text)))] += 1
    top_error = (
        sorted(error_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        if error_counts
        else "sans-erreur"
    )

    parts = [
        "+".join(top_tools) if top_tools else "sans-outil",
        ".".join(top_exts) if top_exts else "sans-fichier",
        top_error,
        "commit" if has_commit else "sans-commit",
    ]
    return "|".join(parts)


def dominant_task_signature(conn: sqlite3.Connection, candidate_id: int) -> str:
    """La `task_signature` la plus fréquente parmi les sessions d'un candidat —
    la classe de tâche à laquelle le skill appartient.

    C'est la clé de récupération pour `ghost retrieve` (et le registre), PAS la
    signature de détecteur (`outil|motif`) stockée sur le candidat, qui ne
    matcherait jamais une signature de tâche.

    Cas import (`ghost create`) : le candidat n'a AUCUNE session mais porte une
    `task_signature` générée à l'import → on la renvoie telle quelle. Sinon,
    "" si rien de fiable à indexer."""
    row = conn.execute(
        "SELECT session_ids_json, task_signature FROM candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if not row:
        return ""
    session_ids = json.loads(str(row[0])) if row[0] else []
    if not session_ids:
        return str(row[1] or "")  # import : signature générée stockée
    counts = Counter(task_signature(conn, str(sid)) for sid in session_ids)
    if not counts:
        return ""
    # Fréquence décroissante, puis signature (départage déterministe).
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
