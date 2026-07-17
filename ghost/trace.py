"""Reconstruction de trace : candidat + events bruts → texte pour le distillateur.

Garde : prompts humains, tool_use (nom + input tronqué), erreurs INTÉGRALES,
corrections humaines. Jette : contenus volumineux de Read, sorties verbeuses.
Budget ~30k tokens (≈4 chars/token) : si dépassé, on garde les fenêtres autour
des erreurs et des overrides (déjà le cœur de l'évidence) et on coupe les
occurrences excédentaires. Redaction systématique avant retour.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ghost.redact import redact

TOKEN_BUDGET = 30_000
_CHAR_BUDGET = TOKEN_BUDGET * 4
_WINDOW = 10  # events de contexte autour de l'évidence

_CAP_ERROR = 4_000  # erreurs intégrales (cap de sécurité très large)
_CAP_HUMAN = 2_000
_CAP_INPUT = 320
_CAP_BASH = 500
_CAP_RESULT_OK = 200
_CAP_TEXT = 500


@dataclass(slots=True)
class Trace:
    candidate_id: int
    kind: str
    signature: str
    text: str
    est_tokens: int
    n_occurrences: int
    n_occurrences_dropped: int
    redactions: dict[str, int]


def _tool_input_str(payload_json: str | None, tool_name: str | None) -> str:
    if not payload_json:
        return ""
    try:
        inp = json.loads(payload_json).get("input", {})
    except (json.JSONDecodeError, AttributeError):
        return "<payload illisible>"
    if not isinstance(inp, dict):
        return ""
    if tool_name == "Bash":
        return str(inp.get("command", ""))[:_CAP_BASH]
    return json.dumps(inp, ensure_ascii=False)[:_CAP_INPUT]


def _render_event(row: tuple[object, ...]) -> str | None:
    (_eid, seq, role, block_type, tool_name, is_error, is_human, text, payload) = row
    text_s = str(text) if text is not None else ""
    if block_type == "thinking":
        return None
    if block_type == "tool_use":
        inp = _tool_input_str(
            str(payload) if payload else None, str(tool_name) if tool_name else None
        )
        return f"[{seq}] outil {tool_name}: {inp}"
    if block_type == "tool_result":
        if is_error:
            # « Erreurs intégrales » : au-delà du cap, on garde tête ET queue —
            # la ligne décisive d'un traceback Python est à la FIN.
            if len(text_s) > _CAP_ERROR:
                text_s = text_s[:1500] + "\n…[tronqué]…\n" + text_s[-2500:]
            return f"[{seq}] ❌ ERREUR: {text_s}"
        return f"[{seq}] ok: {text_s[:_CAP_RESULT_OK]}"
    if role == "user" and is_human:
        return f"[{seq}] 👤 HUMAIN: {text_s[:_CAP_HUMAN]}"
    if block_type == "text":
        return f"[{seq}] {role}: {text_s[:_CAP_TEXT]}"
    if role == "system":
        return f"[{seq}] system/{block_type}: {text_s[:_CAP_TEXT]}"
    return None


def _occurrence_events(
    conn: sqlite3.Connection, occ: dict[str, object]
) -> list[tuple[object, ...]]:
    """Events de la fenêtre : évidence ± _WINDOW, même thread, ordre seq.

    Résolution par (src_file, src_line) — stables à travers les
    ré-ingestions — avec repli sur les events.id (rowids volatils) pour les
    candidats persistés avant l'ajout des src_refs."""
    anchors: list[tuple[Any, ...]] = []
    src_refs = occ.get("src_refs")
    if isinstance(src_refs, list) and src_refs:
        pairs = [
            (str(r[0]), int(r[1]))
            for r in src_refs
            if isinstance(r, list) and len(r) == 2
        ]
        if pairs:
            clause = " OR ".join("(src_file = ? AND src_line = ?)" for _ in pairs)
            anchors = conn.execute(
                f"SELECT session_id, agent_id, MIN(seq), MAX(seq) FROM events "
                f"WHERE {clause} GROUP BY session_id, agent_id",
                [x for pair in pairs for x in pair],
            ).fetchall()
    if not anchors:
        raw_ids = occ.get("event_ids")
        event_ids = (
            [int(i) for i in raw_ids if isinstance(i, int)]
            if isinstance(raw_ids, list)
            else []
        )
        if not event_ids:
            return []
        placeholders = ",".join("?" * len(event_ids))
        anchors = conn.execute(
            f"SELECT session_id, agent_id, MIN(seq), MAX(seq) FROM events "
            f"WHERE id IN ({placeholders}) GROUP BY session_id, agent_id",
            event_ids,
        ).fetchall()
    rows: list[tuple[object, ...]] = []
    for session_id, agent_id, lo, hi in anchors:
        agent_clause = "agent_id IS NULL" if agent_id is None else "agent_id = ?"
        params: list[object] = [session_id, int(lo) - _WINDOW, int(hi) + _WINDOW]
        if agent_id is not None:
            params.insert(1, agent_id)
        rows.extend(
            conn.execute(
                f"SELECT id, seq, role, block_type, tool_name, is_error, is_human, "
                f"text, payload_json FROM events WHERE session_id = ? AND {agent_clause} "
                f"AND seq BETWEEN ? AND ? ORDER BY seq",
                params,
            ).fetchall()
        )
    return rows


def build_trace(conn: sqlite3.Connection, candidate_id: int) -> Trace:
    row = conn.execute(
        "SELECT kind, signature, evidence_json FROM candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"candidat {candidate_id} introuvable")
    kind, signature, evidence_json = str(row[0]), str(row[1]), str(row[2])
    occurrences = json.loads(evidence_json)

    parts: list[str] = [
        f"CANDIDAT #{candidate_id} [{kind}] signature: {signature}",
    ]
    used = len(parts[0])
    n_kept = 0
    # Les occurrences les plus coûteuses (en échecs/edits) d'abord : la coupe
    # budgétaire sacrifie les moins informatives, pas un préfixe arbitraire.
    ordered = sorted(
        occurrences, key=lambda o: float(o.get("cost") or 0.0), reverse=True
    )
    for occ in ordered:
        meta = occ.get("meta", {})
        meta_s = json.dumps(meta, ensure_ascii=False)[:200]
        header = (
            f"\n=== OCCURRENCE session {str(occ.get('session_id', '?'))[:8]} "
            f"(ground_truth={occ.get('ground_truth')}, meta={meta_s}) ==="
        )
        lines = [header]
        for ev in _occurrence_events(conn, occ):
            rendered = _render_event(ev)
            if rendered:
                lines.append(rendered)
        block = "\n".join(lines)
        if used + len(block) > _CHAR_BUDGET:
            if n_kept == 0:
                # Jamais de trace vide : la première occurrence est tronquée
                # au budget plutôt qu'abandonnée.
                parts.append(block[: _CHAR_BUDGET - used])
                used = _CHAR_BUDGET
                n_kept = 1
            continue  # une occurrence plus petite peut encore tenir
        parts.append(block)
        used += len(block)
        n_kept += 1

    raw = "\n".join(parts)
    clean, redactions = redact(raw)
    return Trace(
        candidate_id=candidate_id,
        kind=kind,
        signature=signature,
        text=clean,
        est_tokens=len(clean) // 4,
        n_occurrences=n_kept,
        n_occurrences_dropped=len(occurrences) - n_kept,
        redactions=redactions,
    )
