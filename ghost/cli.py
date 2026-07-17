"""CLI Ghost Brain : `ghost ingest`, `ghost stats`, `ghost scan`, `ghost show`."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from ghost.db import DEFAULT_DB, connect
from ghost.ingest import DEFAULT_ROOT, Summary, ingest_all, scan_files

app = typer.Typer(add_completion=False, help="Ghost Brain — historique Claude Code → SQLite.")
console = Console()

RootOpt = Annotated[Path, typer.Option(help="Racine des projets Claude Code.")]
DbOpt = Annotated[Path, typer.Option(help="Chemin de la base SQLite.")]


@app.command()
def ingest(
    root: RootOpt = DEFAULT_ROOT,
    db: DbOpt = DEFAULT_DB,
    rebuild: Annotated[
        bool,
        typer.Option(
            "--rebuild",
            help="Vide les tables brutes (events/sessions/agents/files_touched/"
            "ingest_log) et ré-ingère tout. Les tables dérivées (candidates, "
            "skills, deployments) sont intactes.",
        ),
    ] = False,
) -> None:
    """Ingère ~/.claude/projects/**/*.jsonl (idempotent, streaming)."""
    conn = connect(db)
    if rebuild:
        # Claude Code purge les vieux JSONL (cleanupPeriodDays) : un rebuild
        # ne ré-ingère que ce qui existe encore. On refuse de perdre des
        # sessions en silence.
        missing = [
            str(path)
            for (path,) in conn.execute("SELECT path FROM ingest_log")
            if not Path(str(path)).exists()
        ]
        if missing:
            conn.close()
            console.print(
                f"[red]{len(missing)} fichier(s) ingéré(s) ont disparu du disque[/red] "
                "(purge Claude Code ?) — un rebuild PERDRAIT ces sessions "
                "définitivement. Abandon. Exemples :"
            )
            for path in missing[:5]:
                console.print(f"  - {path}")
            raise typer.Exit(1)
        for table in ("files_touched", "events", "agents", "sessions", "ingest_log"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        console.print("[yellow]tables brutes vidées — ré-ingestion complète[/yellow]")
    files = scan_files(root)
    summary = Summary(n_files=len(files))
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("ingestion", total=len(files))
        for source, result in ingest_all(conn, root):
            progress.update(task, advance=1, description=source.path.name[:40])
            if result.status == "unchanged":
                summary.n_unchanged += 1
            elif result.status == "failed":
                summary.n_failed += 1
                summary.failures.append((str(source.path), result.error or "?"))
            else:
                summary.n_ingested += 1
                summary.n_events += result.n_events
                summary.n_skipped_lines += result.n_skipped_lines
    for path, error in summary.failures:
        console.print(f"[yellow]⚠ fichier en échec :[/yellow] {path} — {error}")
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_events_total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    console.print(
        f"\n[bold]{n_sessions}[/bold] sessions · [bold]{n_events_total}[/bold] events en base — "
        f"ce run : {summary.n_ingested} fichiers ingérés, {summary.n_unchanged} inchangés, "
        f"{summary.n_failed} en échec, {summary.n_skipped_lines} lignes corrompues sautées"
    )


@app.command()
def stats(db: DbOpt = DEFAULT_DB) -> None:
    """Sessions, events, top 10 outils, plage de dates."""
    conn = connect(db)
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_agents = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    n_human = conn.execute("SELECT COUNT(*) FROM events WHERE is_human = 1").fetchone()[0]
    date_row = conn.execute("SELECT MIN(started_at), MAX(ended_at) FROM sessions").fetchone()

    console.print(
        f"[bold]{n_sessions}[/bold] sessions · [bold]{n_events}[/bold] events · "
        f"{n_agents} subagents · {n_human} messages humains"
    )
    console.print(f"plage : {date_row[0] or '?'} → {date_row[1] or '?'}\n")

    table = Table(title="Top 10 outils")
    table.add_column("outil")
    table.add_column("appels", justify="right")
    table.add_column("erreurs", justify="right")
    table.add_column("taux", justify="right")
    rows = conn.execute(
        """
        SELECT u.tool_name,
               COUNT(*)                    AS n_use,
               SUM(COALESCE(r.is_error, 0)) AS n_err
        FROM events u
        LEFT JOIN events r
               ON r.tool_use_id = u.tool_use_id AND r.block_type = 'tool_result'
        WHERE u.block_type = 'tool_use' AND u.tool_name IS NOT NULL
        GROUP BY u.tool_name
        ORDER BY n_use DESC
        LIMIT 10
        """
    ).fetchall()
    for tool_name, n_use, n_err in rows:
        n_err = int(n_err or 0)
        rate = f"{100.0 * n_err / int(n_use):.1f}%" if int(n_use) else "-"
        table.add_row(str(tool_name), str(n_use), str(n_err), rate)
    console.print(table)
    conn.close()


@app.command()
def scan(db: DbOpt = DEFAULT_DB) -> None:
    """Détecte les cicatrices (FAILURE_LOOP, HUMAN_OVERRIDE, REPEATED_SEQUENCE)."""
    import json as _json

    from ghost.scan import run_scan

    conn = connect(db)
    merged = run_scan(conn)
    by_kind = {k: sum(1 for c in merged if c.kind == k) for k in
               ("FAILURE_LOOP", "HUMAN_OVERRIDE", "REPEATED_SEQUENCE")}
    console.print(
        f"\n[bold]{len(merged)}[/bold] candidats · "
        f"{by_kind['FAILURE_LOOP']} boucles d'échec · "
        f"{by_kind['HUMAN_OVERRIDE']} corrections · "
        f"{by_kind['REPEATED_SEQUENCE']} répétitions\n"
    )
    rows = conn.execute(
        "SELECT id, kind, signature, score, n_occ, n_sessions, evidence_json, status "
        "FROM candidates ORDER BY score DESC LIMIT 15"
    ).fetchall()
    for cid, kind, signature, score, n_occ, n_sessions, evidence_json, status in rows:
        label = signature
        if kind == "HUMAN_OVERRIDE":
            evidence = _json.loads(evidence_json)
            excerpt = str(evidence[0].get("meta", {}).get("excerpt", "")) if evidence else ""
            label = f"{signature}  «{excerpt[:70]}»"
        flag = "" if status == "new" else f" [{status}]"
        console.print(
            f"  [bold]{cid:4d}[/bold]. [{kind}] {label[:96]}\n"
            f"        score {score:.1f} · {n_occ} occ · {n_sessions} sess{flag}"
        )
    total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    console.print(f"\n  top {min(15, int(total))} affichés · {total} candidats en table")
    conn.close()


@app.command()
def show(
    candidate_id: int,
    db: DbOpt = DEFAULT_DB,
    max_occurrences: Annotated[int, typer.Option(help="Occurrences affichées.")] = 5,
) -> None:
    """Dump l'evidence d'un candidat, events bruts inclus."""
    import json as _json

    conn = connect(db)
    row = conn.execute(
        "SELECT kind, signature, score, n_occ, n_sessions, status, evidence_json "
        "FROM candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        conn.close()
        console.print(f"[red]candidat {candidate_id} introuvable[/red]")
        raise typer.Exit(1)
    kind, signature, score, n_occ, n_sessions, status, evidence_json = row
    console.print(
        f"[bold][{kind}][/bold] {signature}\n"
        f"score {score:.1f} · {n_occ} occ · {n_sessions} sessions · status {status}\n"
    )
    for occ in _json.loads(evidence_json)[:max_occurrences]:
        meta = occ.get("meta", {})
        console.print(
            f"[bold]— session {occ['session_id'][:8]}…[/bold] "
            f"coût {occ.get('cost')} · ground_truth {occ.get('ground_truth')} · {meta}"
        )
        for _eid, seq, role, block_type, tool_name, is_error, text, src_file, src_line in (
            _resolve_evidence(conn, occ)
        ):
            mark = " ❌" if is_error else ""
            head = f"  [{seq}] {role}/{block_type}" + (f" {tool_name}" if tool_name else "") + mark
            console.print(head)
            if text:
                console.print(f"      {str(text)[:600]}")
            console.print(f"      [dim]{src_file}:{src_line}[/dim]")
        console.print()
    conn.close()


_EVENT_COLS = "id, seq, role, block_type, tool_name, is_error, text, src_file, src_line"


def _resolve_evidence(
    conn: sqlite3.Connection, occ: dict[str, object]
) -> list[tuple[object, ...]]:
    """Résout les events d'une occurrence, par (src_file, src_line) —
    stables à travers les ré-ingestions — avec repli sur les events.id
    pour les candidats persistés avant l'ajout des src_refs."""
    src_refs = occ.get("src_refs")
    if isinstance(src_refs, list) and src_refs:
        pairs = [(str(r[0]), int(r[1])) for r in src_refs if isinstance(r, list) and len(r) == 2]
        rows: list[tuple[object, ...]] = []
        for start in range(0, len(pairs), 300):
            chunk = pairs[start : start + 300]
            clause = " OR ".join("(src_file = ? AND src_line = ?)" for _ in chunk)
            params = [x for pair in chunk for x in pair]
            rows.extend(
                conn.execute(
                    f"SELECT {_EVENT_COLS} FROM events WHERE {clause} ORDER BY seq", params
                ).fetchall()
            )
        return rows
    ids = occ.get("event_ids")
    if not isinstance(ids, list) or not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return list(
        conn.execute(
            f"SELECT {_EVENT_COLS} FROM events WHERE id IN ({placeholders}) ORDER BY seq", ids
        ).fetchall()
    )


@app.command()
def distill(
    candidate_id: int,
    db: DbOpt = DEFAULT_DB,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Affiche le prompt et la trace, n'appelle pas.")
    ] = False,
) -> None:
    """Distille un candidat en SKILL.md (~/.ghost/skills/<slug>/)."""
    import anthropic as _anthropic

    from ghost.distill import (
        SYSTEM_PROMPT,
        DistillError,
        default_caller,
    )
    from ghost.distill import (
        distill as run_distill,
    )
    from ghost.redact import RedactionError
    from ghost.trace import build_trace

    conn = connect(db)
    try:
        if dry_run:
            trace = build_trace(conn, candidate_id)
            traces_dir = db.parent / "traces"
            traces_dir.mkdir(parents=True, exist_ok=True)
            trace_path = traces_dir / f"candidate-{candidate_id}.txt"
            trace_path.write_text(trace.text, encoding="utf-8")
            console.print(f"[bold]— PROMPT SYSTÈME —[/bold]\n{SYSTEM_PROMPT}\n")
            console.print(
                f"[bold]— TRACE —[/bold] ~{trace.est_tokens} tokens · "
                f"{trace.n_occurrences} occurrences ({trace.n_occurrences_dropped} coupées) · "
                f"redactions {trace.redactions or 'aucune'} · écrite dans {trace_path}\n"
            )
            console.print(trace.text[:4000])
            return
        result = run_distill(
            conn, candidate_id, caller=default_caller(_anthropic.Anthropic())
        )
    except (DistillError, RedactionError, ValueError) as exc:
        console.print(f"[red]échec distillation :[/red] {exc}")
        raise typer.Exit(1) from exc
    except _anthropic.APIError as exc:
        console.print(f"[red]erreur API :[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    console.print(
        f"[bold]{result.verdict}[/bold] · {result.tokens_in} tokens in · "
        f"{result.tokens_out} out · {result.cost_usd:.3f}$ · "
        f"redactions {result.trace.redactions or 'aucune'}"
    )
    if result.verdict == "SKIP":
        console.print(f"raison : {result.skip_reason}")
        return
    if result.low_value:
        console.print("[yellow]⚠ marqué low_value par l'auto-critique[/yellow]")
    else:
        console.print(f"auto-critique : OUI — « {result.critique_line[:120]} »")
    console.print(f"écrit : {result.skill_path}\n")
    assert result.skill_md is not None
    in_pieges = False
    for line in result.skill_md.splitlines():
        if line.startswith("## "):
            in_pieges = line == "## Pièges"
        style = "bold yellow" if in_pieges else ""
        console.print(line, style=style, highlight=False)


def _set_status(db: Path, candidate_id: int, status: str) -> None:
    conn = connect(db)
    try:
        cur = conn.execute(
            "UPDATE candidates SET status = ? WHERE id = ?", (status, candidate_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            console.print(f"[red]candidat {candidate_id} introuvable[/red]")
            raise typer.Exit(1)
        console.print(f"candidat {candidate_id} → [bold]{status}[/bold]")
    finally:
        conn.close()


@app.command()
def keep(candidate_id: int, db: DbOpt = DEFAULT_DB) -> None:
    """Valide un candidat (déployable via ghost deploy)."""
    _set_status(db, candidate_id, "kept")


@app.command()
def reject(candidate_id: int, db: DbOpt = DEFAULT_DB) -> None:
    """Rejette un candidat (survit aux re-scans, jamais re-proposé)."""
    _set_status(db, candidate_id, "rejected")


@app.command()
def skills(db: DbOpt = DEFAULT_DB) -> None:
    """Liste les skills distillés : verdict, coût, statut, déploiement."""
    conn = connect(db)
    rows = conn.execute(
        """
        SELECT sk.id, sk.candidate_id, COALESCE(sk.slug, '—'), sk.verdict,
               sk.low_value, sk.cost_usd, c.status,
               (SELECT COUNT(*) FROM deployments d WHERE d.skill_id = sk.id)
        FROM skills sk LEFT JOIN candidates c ON c.id = sk.candidate_id
        ORDER BY sk.id
        """
    ).fetchall()
    table = Table(title="Skills distillés")
    for col in ("id", "candidat", "slug", "verdict", "coût", "statut", "déployé"):
        table.add_column(col)
    for sid, cid, slug, verdict, low_value, cost, status, n_dep in rows:
        verdict_s = f"{verdict}{' ⚠low_value' if low_value else ''}"
        table.add_row(
            str(sid), str(cid), str(slug), verdict_s, f"{cost:.3f}$",
            str(status or "?"), "oui" if n_dep else "non",
        )
    console.print(table)
    conn.close()


@app.command()
def deploy(
    db: DbOpt = DEFAULT_DB,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Montre sans écrire.")] = False,
    include_low_value: Annotated[
        bool, typer.Option("--include-low-value", help="Déploie aussi les low_value.")
    ] = False,
    force_global: Annotated[
        list[str] | None,
        typer.Option("--force-global", help="Slug à déployer en global (répétable)."),
    ] = None,
) -> None:
    """Déploie les skills des candidats `kept` vers Claude Code."""
    from ghost.deploy import apply_deploy, plan_deploy

    conn = connect(db)
    try:
        actions = plan_deploy(
            conn,
            include_low_value=include_low_value,
            force_global=frozenset(force_global or []),
        )
        if not actions:
            console.print(
                "rien à déployer — valide d'abord des candidats avec `ghost keep <id>`"
            )
            return
        for action in actions:
            console.print(
                f"  {action.slug} → {action.target_dir} [{action.scope}]"
                + (" ⚠low_value" if action.low_value else "")
            )
        if dry_run:
            console.print("\n(dry-run : rien écrit)")
            return
        apply_deploy(conn, actions)
        console.print(f"\n[bold]{len(actions)}[/bold] skill(s) déployé(s)")
    finally:
        conn.close()


@app.command()
def run(
    db: DbOpt = DEFAULT_DB,
    root: RootOpt = DEFAULT_ROOT,
    budget: Annotated[float, typer.Option(help="Plafond de dépense ($) du run.")] = 2.0,
    top: Annotated[int, typer.Option(help="Nb max de nouveaux candidats distillés.")] = 10,
) -> None:
    """Boucle complète : ingest → scan → distille les nouveaux candidats."""
    import anthropic as _anthropic

    from ghost.distill import default_caller
    from ghost.pipeline import run_pipeline

    conn = connect(db)
    try:
        report = run_pipeline(
            conn, caller=default_caller(_anthropic.Anthropic()),
            root=root, budget_usd=budget, top_n=top,
        )
    finally:
        conn.close()

    console.print(
        f"\n[bold]ghost run[/bold] — {report.n_files_ingested} fichiers ingérés "
        f"({report.n_files_unchanged} inchangés) · {report.n_candidates_total} candidats · "
        f"dépense {report.spent_usd:.2f}$ / {budget:.2f}$\n"
    )
    for item in report.items:
        line = f"  {item.candidate_id:5d} [{item.kind}] {item.signature[:60]}"
        if item.outcome == "SKILL":
            console.print(f"{line}\n        → SKILL {item.slug} ({item.cost_usd:.3f}$)")
        elif item.outcome == "SKIP":
            console.print(f"{line}\n        → SKIP ({item.cost_usd:.3f}$)")
        elif item.outcome == "BUDGET":
            console.print(f"{line}\n        → non distillé (budget épuisé)")
        else:
            console.print(f"{line}\n        → [red]ERREUR[/red] {item.error}")
    console.print(
        "\nTriage : `ghost skills` puis `ghost keep <candidat>` et `ghost deploy`."
    )


@app.command()
def watch(
    db: DbOpt = DEFAULT_DB,
    why: Annotated[
        bool, typer.Option("--why", help="Liste les confondants qui invalident la lecture.")
    ] = False,
) -> None:
    """Signal précoce : sessions exposées aux skills vs baseline (0 inférence)."""
    from ghost.watch import collect, render, render_why

    conn = connect(db)
    try:
        report = collect(conn)
    finally:
        conn.close()
    console.print(render(report), highlight=False)
    if why:
        console.print("\n" + render_why(report), highlight=False)


def main() -> None:
    app()
