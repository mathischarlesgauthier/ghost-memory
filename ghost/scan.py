"""Fusion, scoring et persistance des candidats (`ghost scan`).

Formule de score, simple et documentée :
    score = 2·min(n_sessions, 6)    (récurrence inter-sessions, plafonnée :
                                     l'ubiquité est un anti-signal générique)
          + min(n_occ, cap)         (volume ; cap=10, mais 6 pour les
                                     REPEATED_SEQUENCE qui se comptent en
                                     centaines de fenêtres)
          + 0.5·min(coût_total, 20) (échecs/edits payés, plafonné)
          + 3·part_ground_truth     (occurrences suivies d'un commit réussi)
          + 2·multi_projet          (signature vue dans ≥2 projets)
          + 1·part_interruptions    (bonus Esc, calibré : pas un détecteur)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from ghost.detect import (
    Occurrence,
    commit_timestamps,
    detect_failure_loops,
    detect_human_overrides,
    detect_repeated_sequences,
)

MAX_EVIDENCE_OCCURRENCES = 20


@dataclass(slots=True)
class MergedCandidate:
    kind: str
    signature: str
    score: float
    n_occ: int
    n_sessions: int
    session_ids: list[str]
    evidence: list[dict[str, object]]


def collect_occurrences(conn: sqlite3.Connection) -> list[Occurrence]:
    return (
        detect_failure_loops(conn)
        + detect_human_overrides(conn)
        + detect_repeated_sequences(conn)
    )


def merge_and_score(
    occurrences: list[Occurrence],
    commits_by_session: dict[str, list[str]],
    project_by_session: dict[str, str],
) -> list[MergedCandidate]:
    groups: dict[tuple[str, str], list[Occurrence]] = {}
    for occ in occurrences:
        groups.setdefault((occ.kind, occ.signature), []).append(occ)

    merged: list[MergedCandidate] = []
    for (kind, signature), occs in groups.items():
        sessions = sorted({o.session_id for o in occs})
        projects = {project_by_session.get(s, "?") for s in sessions}
        n_occ = sum(o.count for o in occs)
        total_cost = sum(o.cost for o in occs)
        with_gt = sum(1 for o in occs if _has_commit_after(o, commits_by_session))
        gt_frac = with_gt / len(occs)
        interrupt_frac = sum(1 for o in occs if o.meta.get("interrupt")) / len(occs)
        occ_cap = 6 if kind == "REPEATED_SEQUENCE" else 10
        score = (
            2.0 * min(len(sessions), 6)
            + min(n_occ, occ_cap)
            + 0.5 * min(total_cost, 20.0)
            + 3.0 * gt_frac
            + (2.0 if len(projects) > 1 else 0.0)
            + interrupt_frac
        )
        evidence: list[dict[str, object]] = [
            {
                "session_id": o.session_id,
                "event_ids": o.event_ids,
                "cost": o.cost,
                "ts": o.ts,
                "ground_truth": _has_commit_after(o, commits_by_session),
                "meta": o.meta,
            }
            for o in occs[:MAX_EVIDENCE_OCCURRENCES]
        ]
        merged.append(
            MergedCandidate(
                kind=kind,
                signature=signature,
                score=round(score, 2),
                n_occ=n_occ,
                n_sessions=len(sessions),
                session_ids=sessions,
                evidence=evidence,
            )
        )
    merged.sort(key=lambda c: c.score, reverse=True)
    return merged


def _has_commit_after(occ: Occurrence, commits: dict[str, list[str]]) -> bool:
    if occ.ts is None:
        return False
    return any(ts >= occ.ts for ts in commits.get(occ.session_id, []))


def persist(conn: sqlite3.Connection, merged: list[MergedCandidate]) -> None:
    """Upsert par (kind, signature) — le `status` (triage humain) est
    toujours préservé au re-scan. Les candidats encore `new` qui ne sont
    plus détectés (règles resserrées, ré-ingestion) sont purgés ; les
    candidats triés restent."""
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """
        INSERT INTO candidates (kind, signature, score, n_occ, n_sessions,
                                session_ids_json, evidence_json, created_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, signature) DO UPDATE SET
            score = excluded.score,
            n_occ = excluded.n_occ,
            n_sessions = excluded.n_sessions,
            session_ids_json = excluded.session_ids_json,
            evidence_json = excluded.evidence_json,
            last_seen_at = excluded.last_seen_at
        """,
        [
            (
                c.kind,
                c.signature,
                c.score,
                c.n_occ,
                c.n_sessions,
                json.dumps(c.session_ids),
                json.dumps(c.evidence, ensure_ascii=False),
                now,
                now,
            )
            for c in merged
        ],
    )
    conn.execute(
        "DELETE FROM candidates WHERE status = 'new' "
        "AND (last_seen_at IS NULL OR last_seen_at != ?)",
        (now,),
    )
    conn.commit()


def _occurrence_event_ids(occ: dict[str, object]) -> list[int]:
    ids = occ.get("event_ids")
    return [int(i) for i in ids if isinstance(i, int)] if isinstance(ids, list) else []


def attach_src_refs(conn: sqlite3.Connection, merged: list[MergedCandidate]) -> None:
    """Ajoute à chaque occurrence des références (src_file, src_line),
    stables à travers les ré-ingestions — les events.id sont des rowids
    renumérotés quand un fichier de session grossit et est ré-ingéré."""
    all_ids = sorted(
        {eid for c in merged for occ in c.evidence for eid in _occurrence_event_ids(occ)}
    )
    refs: dict[int, tuple[str, int]] = {}
    for start in range(0, len(all_ids), 500):
        chunk = all_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        for eid, src_file, src_line in conn.execute(
            f"SELECT id, src_file, src_line FROM events WHERE id IN ({placeholders})",
            chunk,
        ):
            refs[int(eid)] = (str(src_file), int(src_line))
    for c in merged:
        for occ in c.evidence:
            occ["src_refs"] = [
                list(refs[eid]) for eid in _occurrence_event_ids(occ) if eid in refs
            ]


def run_scan(conn: sqlite3.Connection) -> list[MergedCandidate]:
    occurrences = collect_occurrences(conn)
    commits = commit_timestamps(conn)
    projects = {sid: proj for sid, proj in conn.execute("SELECT id, project FROM sessions")}
    merged = merge_and_score(occurrences, commits, projects)
    attach_src_refs(conn, merged)
    persist(conn, merged)
    return merged
