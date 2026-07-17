"""`ghost run` — la boucle complète : ingest → scan → distille les nouveaux.

Plafond de dépense par run (validé : 2 $, top 10 candidats). On s'arrête
AVANT un appel si le pire cas (~0,21 $ mesuré sur le corpus réel) ferait
dépasser le plafond. Pas d'auto-déploiement : un skill n'atteint Claude
Code qu'après `ghost keep` + `ghost deploy` (le gate humain fait la
qualité).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ghost.distill import DistillError, LlmCaller, distill
from ghost.ingest import DEFAULT_ROOT, ingest_all
from ghost.redact import RedactionError
from ghost.scan import run_scan

WORST_CASE_PER_DISTILL = 0.21  # coût max observé sur le corpus réel


@dataclass(slots=True)
class PipelineItem:
    candidate_id: int
    kind: str
    signature: str
    score: float
    outcome: str  # 'SKILL' | 'SKIP' | 'ERREUR' | 'BUDGET'
    cost_usd: float = 0.0
    slug: str | None = None
    error: str | None = None


@dataclass(slots=True)
class PipelineReport:
    n_files_ingested: int = 0
    n_files_unchanged: int = 0
    n_candidates_total: int = 0
    spent_usd: float = 0.0
    items: list[PipelineItem] = field(default_factory=list)


def run_pipeline(
    conn: sqlite3.Connection,
    *,
    caller: LlmCaller,
    root: Path = DEFAULT_ROOT,
    budget_usd: float = 2.0,
    top_n: int = 10,
) -> PipelineReport:
    report = PipelineReport()

    for _source, result in ingest_all(conn, root):
        if result.status == "unchanged":
            report.n_files_unchanged += 1
        elif result.status == "ingested":
            report.n_files_ingested += 1

    merged = run_scan(conn)
    report.n_candidates_total = len(merged)

    rows = conn.execute(
        """
        SELECT id, kind, signature, score FROM candidates
        WHERE status = 'new'
          AND id NOT IN (SELECT candidate_id FROM skills)
        ORDER BY score DESC LIMIT ?
        """,
        (top_n,),
    ).fetchall()

    for cid, kind, signature, score in rows:
        item = PipelineItem(
            candidate_id=int(cid), kind=str(kind), signature=str(signature),
            score=float(score), outcome="BUDGET",
        )
        if report.spent_usd + WORST_CASE_PER_DISTILL > budget_usd:
            report.items.append(item)
            continue
        try:
            distilled = distill(conn, int(cid), caller=caller)
        except (DistillError, RedactionError) as exc:
            item.outcome = "ERREUR"
            item.error = str(exc)
        else:
            item.outcome = distilled.verdict
            item.cost_usd = distilled.cost_usd
            item.slug = distilled.slug
            report.spent_usd += distilled.cost_usd
        report.items.append(item)
    return report
