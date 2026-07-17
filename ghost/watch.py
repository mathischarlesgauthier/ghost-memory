"""`ghost watch` — instrumentation passive, 0 inférence.

Compare les sessions EXPOSÉES (un skill déployé apparaissait dans le
skill_listing de leur thread principal) à la baseline historique,
appariées par task_signature. Signal précoce, jamais « prouvé » : la
sortie dit « signal » ou « pas de signal », et `--why` liste ce qui
invalide la lecture.

Durcissements issus de la revue adversariale :
- exposition vérifiée par slug ET description du SKILL.md déployé (un
  slug qui collisionne avec un skill builtin ne contamine pas les
  cohortes), avec garde temporelle started_at ≥ premier deploy ;
- dates comparées en datetime (les formats ISO diffèrent entre
  deployments `+00:00` et sessions `Z`) ;
- métriques dédupliquées du REPLAY des sessions reprises (445
  tool_use_id partagés mesurés) : un event rejoué ne compte que dans la
  session qui l'a produit en premier ;
- tokens = SUM(MAX(usage_out) par msg_id) — les snapshots sont cumulatifs.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from ghost.detect import INTERRUPT_MARKER, commit_timestamps
from ghost.signature import task_signature

RECENT_BASELINE_DAYS = 14
MIN_N_SIGNAL = 3
SIGNAL_DELTA = 0.20  # |Δ médiane| relatif pour afficher ▼/▲

_SKILL_LINE_RE = re.compile(r"^- ([\w-]+): (.*)$", re.M)
_DESC_PREFIX_LEN = 30
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class SessionMetrics:
    session_id: str
    signature: str
    started_at: str | None
    claude_version: str | None
    cohort: str  # 'baseline' | 'exposee' | 'post_non_exposee'
    in_recent_baseline: bool
    n_human: int
    n_tool_use: int
    n_tool_err: int
    tokens_out: int
    has_commit: bool
    duration_min: float | None


@dataclass(slots=True)
class WatchReport:
    deployed_slugs: set[str] = field(default_factory=set)
    first_deploy_at: str | None = None
    sessions: list[SessionMetrics] = field(default_factory=list)
    excluded_signatures: list[str] = field(default_factory=list)
    n_replayed_deduped: int = 0


def deployed_markers(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str | None], str | None]:
    """slug → préfixe de description du SKILL.md déployé (None si le
    fichier n'est pas lisible), + date du premier deploy."""
    markers: dict[str, str | None] = {}
    first: str | None = None
    for slug, target, deployed_at in conn.execute(
        "SELECT sk.slug, d.target_path, d.deployed_at FROM deployments d "
        "JOIN skills sk ON sk.id = d.skill_id WHERE sk.slug IS NOT NULL"
    ):
        slug_s = str(slug)
        if first is None or str(deployed_at) < first:
            first = str(deployed_at)
        if slug_s in markers and markers[slug_s] is not None:
            continue
        description: str | None = None
        try:
            text = Path(str(target)).read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = re.search(r"^description: (.+)$", text, re.M)
        if match:
            description = match.group(1).strip()[:_DESC_PREFIX_LEN]
        markers[slug_s] = description
    return markers, first


def _session_exposed(
    conn: sqlite3.Connection, session_id: str, markers: dict[str, str | None]
) -> bool:
    """Exposée ssi le skill_listing du THREAD PRINCIPAL contient un slug
    déployé dont la description matche celle du SKILL.md déployé."""
    for (text,) in conn.execute(
        "SELECT text FROM events WHERE session_id = ? AND agent_id IS NULL "
        "AND block_type = 'skill_listing'",
        (session_id,),
    ):
        for slug, rest in _SKILL_LINE_RE.findall(str(text or "")):
            if slug not in markers:
                continue
            expected = markers[slug]
            if expected is None or rest.strip().startswith(expected):
                return True
    return False


def _duration_minutes(started: str | None, ended: str | None) -> float | None:
    t0, t1 = _parse_ts(started), _parse_ts(ended)
    if t0 is None or t1 is None:
        return None
    return round((t1 - t0).total_seconds() / 60, 1)


def _first_owner_maps(
    conn: sqlite3.Connection, session_order: dict[str, int]
) -> tuple[dict[str, str], dict[str, str], int]:
    """Attribue chaque tool_use_id et msg_id à la PREMIÈRE session (par
    date de début) qui le porte : les sessions reprises rejouent
    l'historique du parent, sans dédup leurs médianes sont gonflées."""
    tuid_owner: dict[str, str] = {}
    msg_owner: dict[str, str] = {}
    n_replayed = 0

    def rank(sid: str) -> int:
        return session_order.get(sid, len(session_order))

    for sid, tuid in conn.execute(
        "SELECT DISTINCT session_id, tool_use_id FROM events "
        "WHERE tool_use_id IS NOT NULL AND block_type = 'tool_use'"
    ):
        key, session = str(tuid), str(sid)
        if key not in tuid_owner or rank(session) < rank(tuid_owner[key]):
            if key in tuid_owner:
                n_replayed += 1
            tuid_owner[key] = session
        elif tuid_owner[key] != session:
            n_replayed += 1
    for sid, mid in conn.execute(
        "SELECT DISTINCT session_id, msg_id FROM events WHERE msg_id IS NOT NULL"
    ):
        key, session = str(mid), str(sid)
        if key not in msg_owner or rank(session) < rank(msg_owner[key]):
            msg_owner[key] = session
    return tuid_owner, msg_owner, n_replayed


def collect(conn: sqlite3.Connection) -> WatchReport:
    report = WatchReport()
    markers, report.first_deploy_at = deployed_markers(conn)
    report.deployed_slugs = set(markers)
    commits = commit_timestamps(conn)
    deploy_dt = _parse_ts(report.first_deploy_at)
    recent_cutoff = deploy_dt - timedelta(days=RECENT_BASELINE_DAYS) if deploy_dt else None

    session_rows = conn.execute(
        "SELECT id, started_at, ended_at, claude_version FROM sessions"
    ).fetchall()
    ordered = sorted(
        (str(r[0]) for r in session_rows),
        key=lambda sid: next(
            (
                _parse_ts(str(r[1])) or _FAR_FUTURE
                for r in session_rows
                if str(r[0]) == sid
            ),
            _FAR_FUTURE,
        ),
    )
    session_order = {sid: i for i, sid in enumerate(ordered)}
    tuid_owner, msg_owner, report.n_replayed_deduped = _first_owner_maps(
        conn, session_order
    )

    for sid, started, ended, version in session_rows:
        session_id = str(sid)
        started_dt = _parse_ts(str(started) if started else None)
        exposed = (
            _session_exposed(conn, session_id, markers)
            and deploy_dt is not None
            and started_dt is not None
            and started_dt >= deploy_dt
        )
        if exposed:
            cohort = "exposee"
        elif deploy_dt and started_dt and started_dt > deploy_dt:
            cohort = "post_non_exposee"
        else:
            cohort = "baseline"

        n_human = conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ? AND is_human = 1 "
            "AND agent_id IS NULL AND (text IS NULL OR text NOT LIKE ?)",
            (session_id, f"{INTERRUPT_MARKER}%"),
        ).fetchone()[0]
        n_tool_use = sum(
            1
            for (tuid,) in conn.execute(
                "SELECT tool_use_id FROM events WHERE session_id = ? "
                "AND block_type = 'tool_use'",
                (session_id,),
            )
            if tuid is None or tuid_owner.get(str(tuid)) == session_id
        )
        n_tool_err = sum(
            1
            for (tuid,) in conn.execute(
                "SELECT tool_use_id FROM events WHERE session_id = ? "
                "AND block_type = 'tool_result' AND is_error = 1",
                (session_id,),
            )
            if tuid is None or tuid_owner.get(str(tuid)) == session_id
        )
        tokens_by_msg: dict[str, int] = {}
        direct_tokens = 0
        for mid, usage in conn.execute(
            "SELECT msg_id, MAX(usage_out) FROM events WHERE session_id = ? "
            "AND usage_out IS NOT NULL GROUP BY msg_id",
            (session_id,),
        ):
            if mid is None:
                direct_tokens += int(usage or 0)
            elif msg_owner.get(str(mid)) == session_id:
                tokens_by_msg[str(mid)] = int(usage or 0)
        report.sessions.append(
            SessionMetrics(
                session_id=session_id,
                signature=task_signature(conn, session_id),
                started_at=str(started) if started else None,
                claude_version=str(version) if version else None,
                cohort=cohort,
                in_recent_baseline=(
                    cohort == "baseline"
                    and recent_cutoff is not None
                    and started_dt is not None
                    and started_dt >= recent_cutoff
                ),
                n_human=int(n_human),
                n_tool_use=n_tool_use,
                n_tool_err=n_tool_err,
                tokens_out=sum(tokens_by_msg.values()) + direct_tokens,
                has_commit=session_id in commits,
                duration_min=_duration_minutes(
                    str(started) if started else None, str(ended) if ended else None
                ),
            )
        )
    return report


@dataclass(slots=True)
class ClassView:
    signature: str
    baseline: list[SessionMetrics]
    recent_baseline: list[SessionMetrics]
    exposed: list[SessionMetrics]


def matched_classes(report: WatchReport) -> tuple[list[ClassView], list[str]]:
    """Classes avec contrepartie dans les DEUX cohortes ; le reste est
    exclu et listé, jamais extrapolé."""
    by_sig: dict[str, list[SessionMetrics]] = {}
    for sm in report.sessions:
        by_sig.setdefault(sm.signature, []).append(sm)
    matched: list[ClassView] = []
    excluded: list[str] = []
    for signature, group in sorted(by_sig.items()):
        baseline = [s for s in group if s.cohort == "baseline"]
        exposed = [s for s in group if s.cohort == "exposee"]
        if baseline and exposed:
            matched.append(
                ClassView(
                    signature=signature,
                    baseline=baseline,
                    recent_baseline=[s for s in baseline if s.in_recent_baseline],
                    exposed=exposed,
                )
            )
        else:
            excluded.append(
                f"{signature} (baseline={len(baseline)}, exposées={len(exposed)})"
            )
    return matched, excluded


def _fmt_cohort(label: str, sessions: list[SessionMetrics]) -> str:
    n = len(sessions)
    if n == 0:
        return f"    {label:<16} n=0"
    med_h = median(s.n_human for s in sessions)
    med_t = median(s.n_tool_use for s in sessions)
    med_tok = median(s.tokens_out for s in sessions)
    commits = sum(1 for s in sessions if s.has_commit)
    med_err = median(s.n_tool_err for s in sessions)
    return (
        f"    {label:<16} n={n:<3} médiane {med_h:.0f} msg humains · "
        f"{med_t:.0f} tool_use · {med_err:.0f} err · "
        f"{med_tok / 1000:.0f}k tok · commit {commits}/{n}"
    )


def _signal_line(cls: ClassView) -> str:
    nb, ne = len(cls.baseline), len(cls.exposed)
    if nb < MIN_N_SIGNAL or ne < MIN_N_SIGNAL:
        return f"    signal: n insuffisant (baseline={nb}, exposées={ne})"
    marks: list[str] = []
    getters: list[tuple[str, Callable[[SessionMetrics], int]]] = [
        ("tool_use", lambda s: s.n_tool_use),
        ("err", lambda s: s.n_tool_err),
        ("tok", lambda s: s.tokens_out),
    ]
    for label, getter in getters:
        base = float(median(getter(s) for s in cls.baseline))
        expo = float(median(getter(s) for s in cls.exposed))
        if base <= 0:
            # Médiane baseline nulle (cas normal pour les erreurs) : une
            # apparition côté exposées est un signal, pas un silence.
            if expo > 0:
                marks.append(f"▲ {label} (0 → {expo:.0f})")
            continue
        delta = (expo - base) / base
        if delta <= -SIGNAL_DELTA:
            marks.append(f"▼ {label}")
        elif delta >= SIGNAL_DELTA:
            marks.append(f"▲ {label}")
    if not marks:
        return "    signal: plat"
    caveat = " (n faible, non causal)" if min(nb, ne) < 10 else " (non causal)"
    return "    signal: " + " · ".join(marks) + caveat


def render(report: WatchReport) -> str:
    matched, excluded = matched_classes(report)
    by_cohort = Counter(s.cohort for s in report.sessions)
    lines = [
        f"skills déployés : {', '.join(sorted(report.deployed_slugs)) or 'aucun'}",
        f"premier deploy : {report.first_deploy_at or '—'}",
        f"sessions : {by_cohort.get('baseline', 0)} baseline · "
        f"{by_cohort.get('exposee', 0)} exposées · "
        f"{by_cohort.get('post_non_exposee', 0)} post non exposées",
        "",
    ]
    if not matched:
        lines.append("Aucune classe appariée (baseline ET exposées) — pas de signal.")
    for cls in matched:
        lines.append(f"Classe: {cls.signature}")
        lines.append(_fmt_cohort("baseline", cls.baseline))
        if cls.recent_baseline:
            lines.append(_fmt_cohort("baseline 14j", cls.recent_baseline))
        lines.append(_fmt_cohort("exposées", cls.exposed))
        lines.append(_signal_line(cls))
        lines.append("")
    if excluded:
        lines.append(f"classes exclues (sans contrepartie) : {len(excluded)}")
        lines.extend(f"  - {sig}" for sig in excluded[:10])
        if len(excluded) > 10:
            lines.append(f"  … et {len(excluded) - 10} autres")
    return "\n".join(lines)


def render_why(report: WatchReport) -> str:
    """Confondants : ce que ce code ne peut PAS exclure, il le dit."""
    baseline_versions = Counter(
        s.claude_version or "?" for s in report.sessions if s.cohort == "baseline"
    )
    exposed_versions = Counter(
        s.claude_version or "?" for s in report.sessions if s.cohort == "exposee"
    )
    base_sigs = Counter(
        s.signature for s in report.sessions if s.cohort == "baseline"
    )
    expo_sigs = Counter(s.signature for s in report.sessions if s.cohort == "exposee")
    lines = [
        "CONFONDANTS — pourquoi cette lecture peut être fausse :",
        "",
        "1. Le harnais a évolué entre les cohortes (MESURÉ) :",
        f"   baseline : {dict(baseline_versions.most_common(5))}",
        f"   exposées : {dict(exposed_versions.most_common(5))}",
        "   Exemple prouvé : depuis 2.1.212, Claude Code auto-récupère les",
        "   edits périmés (staleRecovered) — l'erreur cible d'un des skills",
        "   déployés a partiellement disparu SANS le skill.",
        "",
        "2. La nature des tâches a changé (MESURÉ, non exclu) :",
        f"   top signatures baseline : {[s for s, _ in base_sigs.most_common(3)]}",
        f"   top signatures exposées : {[s for s, _ in expo_sigs.most_common(3)]}",
        "",
        "3. Les sessions reprises rejouent l'historique du parent (MESURÉ,",
        f"   dédupliqué : {report.n_replayed_deduped} tool_use rejoués exclus des",
        "   comptes — mais la reprise elle-même reste un biais de sélection).",
        "",
        "4. Tu progresses toi-même entre les périodes (NON EXCLUABLE ici).",
        "",
        "5. Effet de nouveauté (NON EXCLUABLE) : juste après un deploy, tu",
        "   surveilles plus, tu interviens différemment.",
        "",
        "6. n faibles : toute classe sous n=5 par cohorte est du bruit",
        "   possible. Seul le lot 6 (replay contrôlé) peut produire du causal.",
    ]
    return "\n".join(lines)
