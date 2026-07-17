"""`ghost validate <skill_id>` — le chiffre causal, mesuré en rejouant.

Protocole (lu deux fois) : n≥3 runs par condition et par cas, ordre
ALTERNÉ (jamais tous les « sans » puis tous les « avec »), agrégation en
médiane avec écart ET n, distributions qui se recouvrent → « pas de lift
mesurable » (c'est un résultat). Chaque run est persisté (reprise
possible) ; arrêt net au dépassement du plafond.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from ghost.replay import (
    PER_RUN_BUDGET_USD,
    ReplayError,
    RunMetrics,
    commit_exists,
    inject_skill,
    run_replay,
    sandbox,
)
from ghost.signature import task_signature

MIN_CASES = 3
EST_COST_PER_RUN = 0.35  # milieu de fourchette mesurée (0,10-0,60 $)

_COMMIT_RESULT_RE = re.compile(r"^\[([\w/.\-]+) ([0-9a-f]{7,40})\]")


@dataclass(slots=True)
class ReplayCase:
    case_id: str
    session_id: str
    prompt: str
    repo: Path
    base_commit: str
    head_end: str
    signature: str


@dataclass(slots=True)
class SkillInfo:
    skill_id: int
    slug: str
    source: Path
    candidate_id: int


def skill_info(conn: sqlite3.Connection, skill_id: int) -> SkillInfo:
    row = conn.execute(
        "SELECT slug, path, candidate_id FROM skills WHERE id = ? AND verdict = 'SKILL'",
        (skill_id,),
    ).fetchone()
    if row is None or row[1] is None:
        raise ReplayError(f"skill {skill_id} introuvable ou sans SKILL.md")
    return SkillInfo(
        skill_id=skill_id, slug=str(row[0]), source=Path(str(row[1])),
        candidate_id=int(row[2]),
    )


def _skill_class_signatures(conn: sqlite3.Connection, candidate_id: int) -> set[str]:
    row = conn.execute(
        "SELECT session_ids_json FROM candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    session_ids = json.loads(str(row[0])) if row and row[0] else []
    return {task_signature(conn, str(sid)) for sid in session_ids}


def _session_commits(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """Hashes des commits réussis de la session, dans l'ordre, extraits des
    tool_results `[branche hash] …`."""
    hashes: list[str] = []
    for (text,) in conn.execute(
        """
        SELECT r.text FROM events u
        JOIN events r ON r.tool_use_id = u.tool_use_id AND r.block_type = 'tool_result'
             AND r.src_file = u.src_file AND COALESCE(r.is_error, 0) = 0
        WHERE u.block_type = 'tool_use' AND u.tool_name = 'Bash'
              AND u.session_id = ? AND u.payload_json LIKE '%git commit%'
              AND r.text LIKE '[%'
        ORDER BY u.seq
        """,
        (session_id,),
    ):
        match = _COMMIT_RESULT_RE.match(str(text or ""))
        if match:
            hashes.append(match.group(2))
    return hashes


def _class_touched_files(conn: sqlite3.Connection, candidate_id: int) -> set[str]:
    row = conn.execute(
        "SELECT session_ids_json FROM candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    session_ids = json.loads(str(row[0])) if row and row[0] else []
    if not session_ids:
        return set()
    placeholders = ",".join("?" * len(session_ids))
    return {
        str(path)
        for (path,) in conn.execute(
            f"SELECT DISTINCT f.path FROM files_touched f "
            f"JOIN events e ON e.id = f.event_id "
            f"WHERE e.session_id IN ({placeholders})",
            [str(s) for s in session_ids],
        )
    }


def _session_touched_files(conn: sqlite3.Connection, session_id: str) -> set[str]:
    return {
        str(path)
        for (path,) in conn.execute(
            "SELECT DISTINCT f.path FROM files_touched f "
            "JOIN events e ON e.id = f.event_id WHERE e.session_id = ?",
            (session_id,),
        )
    }


def eligible_cases(
    conn: sqlite3.Connection, skill: SkillInfo, *, match: str = "strict"
) -> tuple[list[ReplayCase], dict[str, int]]:
    """Cas rejouables + comptes d'exclusion (rapportés, jamais extrapolés).

    match='strict' : task_signature identique à la classe du skill.
    match='souple' (arbitré) : signature identique OU ≥1 fichier touché en
    commun avec les sessions sources du skill."""
    class_sigs = _skill_class_signatures(conn, skill.candidate_id)
    class_files = _class_touched_files(conn, skill.candidate_id) if match == "souple" else set()
    counts = {
        "sessions_examinees": 0, "hors_signature": 0, "sans_commit_extractible": 0,
        "sans_prompt": 0, "repo_ou_commit_absent": 0,
    }
    cases: list[ReplayCase] = []
    for sid, cwd in conn.execute("SELECT id, cwd FROM sessions WHERE cwd IS NOT NULL"):
        session_id = str(sid)
        counts["sessions_examinees"] += 1
        sig_ok = task_signature(conn, session_id) in class_sigs
        files_ok = (
            match == "souple"
            and bool(class_files & _session_touched_files(conn, session_id))
        )
        if not sig_ok and not files_ok:
            counts["hors_signature"] += 1
            continue
        hashes = _session_commits(conn, session_id)
        if not hashes:
            counts["sans_commit_extractible"] += 1
            continue
        prompt_row = conn.execute(
            "SELECT text FROM events WHERE session_id = ? AND is_human = 1 "
            "AND agent_id IS NULL AND LENGTH(COALESCE(text, '')) > 50 "
            "ORDER BY seq LIMIT 1",
            (session_id,),
        ).fetchone()
        if prompt_row is None:
            counts["sans_prompt"] += 1
            continue
        repo = Path(str(cwd))
        base = f"{hashes[0]}~1"
        if not (repo / ".git").exists() or not commit_exists(repo, base):
            counts["repo_ou_commit_absent"] += 1
            continue
        cases.append(
            ReplayCase(
                case_id=session_id[:8],
                session_id=session_id,
                prompt=str(prompt_row[0]),
                repo=repo,
                base_commit=base,
                head_end=hashes[-1],
                signature=task_signature(conn, session_id),
            )
        )
    return cases, counts


def rank_cases_by_motif(
    conn: sqlite3.Connection, skill: SkillInfo, cases: list[ReplayCase]
) -> list[ReplayCase]:
    """Trie les cas par pertinence : les sessions contenant l'erreur cible
    du skill (motif de la signature du candidat) d'abord — les plus
    probantes pour un A/B."""
    row = conn.execute(
        "SELECT signature FROM candidates WHERE id = ?", (skill.candidate_id,)
    ).fetchone()
    signature = str(row[0]) if row else ""
    motif = signature.split("|", 1)[1][:40] if "|" in signature else signature[:40]

    def relevance(case: ReplayCase) -> int:
        if not motif:
            return 0
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ? "
                "AND block_type = 'tool_result' AND is_error = 1 "
                "AND text LIKE ?",
                (case.session_id, f"%{motif}%"),
            ).fetchone()[0]
        )

    return sorted(cases, key=relevance, reverse=True)


# --------------------------------------------------------------------------
# Protocole


Runner = Callable[..., RunMetrics]
SandboxFactory = Callable[[Path, str], AbstractContextManager[Path]]


@dataclass(slots=True)
class RunRecord:
    case_id: str
    condition: str
    run_idx: int
    metrics: RunMetrics


@dataclass(slots=True)
class ValidationReport:
    skill_id: int
    slug: str
    records: list[RunRecord] = field(default_factory=list)
    spent_usd: float = 0.0
    stopped_on_budget: bool = False
    errors: list[str] = field(default_factory=list)


def _existing_runs(conn: sqlite3.Connection, skill_id: int) -> set[tuple[str, str, int]]:
    return {
        (str(c), str(cond), int(i))
        for c, cond, i in conn.execute(
            "SELECT case_id, condition, run_idx FROM replays WHERE skill_id = ?",
            (skill_id,),
        )
    }


def _persist_run(
    conn: sqlite3.Connection, skill_id: int, record: RunRecord
) -> None:
    conn.execute(
        """
        INSERT INTO replays (skill_id, case_id, condition, run_idx, metrics_json,
                             cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(skill_id, case_id, condition, run_idx) DO UPDATE SET
            metrics_json = excluded.metrics_json, cost_usd = excluded.cost_usd
        """,
        (
            skill_id, record.case_id, record.condition, record.run_idx,
            json.dumps(_metrics_dict(record.metrics), ensure_ascii=False),
            record.metrics.cost_usd,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()


def _metrics_dict(metrics: RunMetrics) -> dict[str, object]:
    return {
        "turns": metrics.turns, "cost_usd": metrics.cost_usd,
        "duration_ms": metrics.duration_ms, "output_tokens": metrics.output_tokens,
        "tool_errors": metrics.tool_errors, "new_commits": metrics.new_commits,
        "changed_lines": metrics.changed_lines, "success": metrics.success,
        "is_error": metrics.is_error, "denials": metrics.denials,
        "timed_out": metrics.timed_out, "jsonl_missing": metrics.jsonl_missing,
    }


@contextmanager
def _default_sandbox(repo: Path, base_commit: str) -> Iterator[Path]:
    with sandbox(repo, base_commit) as work:
        yield work


def run_validation(
    conn: sqlite3.Connection,
    skill: SkillInfo,
    cases: list[ReplayCase],
    *,
    max_cost_usd: float,
    n_per_condition: int = 3,
    per_run_budget: float = PER_RUN_BUDGET_USD,
    runner: Runner = run_replay,
    sandbox_factory: SandboxFactory = _default_sandbox,
    on_progress: Callable[[str], None] = lambda _msg: None,
) -> ValidationReport:
    report = ValidationReport(skill_id=skill.skill_id, slug=skill.slug)
    done = _existing_runs(conn, skill.skill_id)
    report.spent_usd = float(
        conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM replays WHERE skill_id = ?",
            (skill.skill_id,),
        ).fetchone()[0]
    )
    # Ordre alterné : sans, avec, sans, avec… par cas.
    schedule: list[tuple[ReplayCase, str, int]] = []
    for case in cases:
        for run_idx in range(n_per_condition):
            schedule.append((case, "sans", run_idx))
            schedule.append((case, "avec", run_idx))

    consecutive_errors = 0
    for case, condition, run_idx in schedule:
        if (case.case_id, condition, run_idx) in done:
            continue
        if report.spent_usd + per_run_budget > max_cost_usd:
            report.stopped_on_budget = True
            on_progress(
                f"arrêt budget : {report.spent_usd:.2f}$ dépensés, "
                f"prochain run risquerait de dépasser {max_cost_usd:.2f}$"
            )
            break
        if consecutive_errors >= 3:
            report.errors.append("3 échecs consécutifs — arrêt du protocole")
            break
        on_progress(f"case {case.case_id} · {condition} · run {run_idx + 1}")
        try:
            with sandbox_factory(case.repo, case.base_commit) as work:
                if condition == "avec":
                    inject_skill(work, skill.source, skill.slug)
                metrics = runner(work, case.prompt, budget_usd=per_run_budget)
        except ReplayError as exc:
            # L'appel API a pu dépenser AVANT l'échec du post-traitement :
            # on provisionne le pire cas pour que le plafond reste honnête.
            report.errors.append(f"{case.case_id}/{condition}/{run_idx}: {exc}")
            report.spent_usd += per_run_budget
            consecutive_errors += 1
            continue
        consecutive_errors = 0
        record = RunRecord(case.case_id, condition, run_idx, metrics)
        _persist_run(conn, skill.skill_id, record)
        report.records.append(record)
        report.spent_usd += metrics.cost_usd
    return report


# --------------------------------------------------------------------------
# Agrégation


@dataclass(slots=True)
class Lift:
    metric: str
    baseline_median: float
    exposed_median: float
    delta_pct: float | None
    consistent: bool  # tous les cas bougent dans le même sens


@dataclass(slots=True)
class LiftReport:
    skill_id: int
    n_cases: int
    n_runs: int
    cost_usd: float
    lifts: list[Lift] = field(default_factory=list)
    success_sans: tuple[int, int] = (0, 0)
    success_avec: tuple[int, int] = (0, 0)
    verdict: str = "pas de lift mesurable"


def aggregate(conn: sqlite3.Connection, skill_id: int) -> LiftReport:
    rows = conn.execute(
        "SELECT case_id, condition, metrics_json, cost_usd FROM replays "
        "WHERE skill_id = ?",
        (skill_id,),
    ).fetchall()
    by_case: dict[str, dict[str, list[dict[str, object]]]] = {}
    total_cost = 0.0
    for case_id, condition, metrics_json, cost in rows:
        by_case.setdefault(str(case_id), {}).setdefault(str(condition), []).append(
            json.loads(str(metrics_json))
        )
        total_cost += float(cost)
    complete_cases = {
        case_id: conds
        for case_id, conds in by_case.items()
        if conds.get("sans") and conds.get("avec")
    }
    report = LiftReport(
        skill_id=skill_id, n_cases=len(complete_cases), n_runs=len(rows),
        cost_usd=round(total_cost, 2),
    )
    if not complete_cases:
        return report

    def _num(m: dict[str, object], key: str) -> float:
        value = m.get(key, 0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    # tool_errors n'est agrégeable que si TOUS les transcripts ont été
    # retrouvés — « pas de JSONL » n'est pas « pas d'erreur ».
    errors_valid = not any(
        m.get("jsonl_missing")
        for conds in complete_cases.values()
        for runs_list in conds.values()
        for m in runs_list
    )
    metrics_list = ["turns", "output_tokens", "duration_ms"]
    if errors_valid:
        metrics_list.insert(2, "tool_errors")
    for metric in metrics_list:
        deltas: list[float] = []
        sans_meds: list[float] = []
        avec_meds: list[float] = []
        for conds in complete_cases.values():
            med_sans = median(_num(m, metric) for m in conds["sans"])
            med_avec = median(_num(m, metric) for m in conds["avec"])
            sans_meds.append(med_sans)
            avec_meds.append(med_avec)
            deltas.append(med_avec - med_sans)
        pooled_sans = median(sans_meds)
        pooled_avec = median(avec_meds)
        delta_pct = (
            (pooled_avec - pooled_sans) / pooled_sans if pooled_sans > 0 else None
        )
        consistent = all(d < 0 for d in deltas) or all(d > 0 for d in deltas)
        report.lifts.append(
            Lift(
                metric=metric, baseline_median=pooled_sans,
                exposed_median=pooled_avec, delta_pct=delta_pct,
                consistent=consistent,
            )
        )
    sans_ok = sum(
        1 for c in complete_cases.values() for m in c["sans"] if m.get("success")
    )
    sans_n = sum(len(c["sans"]) for c in complete_cases.values())
    avec_ok = sum(
        1 for c in complete_cases.values() for m in c["avec"] if m.get("success")
    )
    avec_n = sum(len(c["avec"]) for c in complete_cases.values())
    report.success_sans = (sans_ok, sans_n)
    report.success_avec = (avec_ok, avec_n)

    # Verdict : lift seulement si cohérent entre cas ET |Δ| > 20 % pooled.
    # duration_ms est exclu du verdict (bruit de latence mur-à-mur >20 %
    # courant — il resterait affiché mais ne fonde jamais un « lift »).
    # Distributions qui se recouvrent / signes mixtes → « pas de lift
    # mesurable » — c'est un résultat, pas un échec.
    strong = [
        lift for lift in report.lifts
        if lift.metric != "duration_ms"
        and lift.consistent
        and lift.delta_pct is not None
        and abs(lift.delta_pct) > 0.20
    ]
    if strong:
        report.verdict = "lift " + " · ".join(
            f"{'-' if (lift.delta_pct or 0) < 0 else '+'}"
            f"{abs(lift.delta_pct or 0) * 100:.0f}% {lift.metric}"
            for lift in strong
        )
    return report


def write_lift_frontmatter(skill_md: Path, report: LiftReport) -> None:
    """Écrit le lift mesuré dans le frontmatter du SKILL.md source."""
    text = skill_md.read_text(encoding="utf-8")
    summary = (
        f"{report.verdict} (n={report.n_cases} cas, {report.n_runs} runs, "
        f"{report.cost_usd:.2f}$)"
    )
    lift_line = f"lift: {summary}"
    if re.search(r"^lift: .*$", text, re.M):
        text = re.sub(r"^lift: .*$", lift_line, text, count=1, flags=re.M)
    else:
        lines = text.splitlines(keepends=True)
        closes = [i for i, ln in enumerate(lines) if ln.rstrip() == "---"]
        if len(closes) >= 2:  # insérer avant le '---' fermant du frontmatter
            lines.insert(closes[1], f"{lift_line}\n")
            text = "".join(lines)
    skill_md.write_text(text, encoding="utf-8")
