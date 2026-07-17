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
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if n_events == 0:
        conn.close()
        console.print(
            "base vide — lance d'abord `ghost ingest` (puis `ghost doctor` en cas "
            "de doute sur ton historique Claude Code)."
        )
        return
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


@app.command()
def validate(
    skill_id: int,
    db: DbOpt = DEFAULT_DB,
    max_cost: Annotated[float, typer.Option(help="Plafond de dépense ($) du replay.")] = 8.0,
    runs: Annotated[int, typer.Option(help="Runs par condition et par cas (≥3).")] = 3,
    match: Annotated[
        str, typer.Option(help="Matching des cas : strict | souple (fichiers communs).")
    ] = "strict",
    max_cases: Annotated[
        int,
        typer.Option(help="Limite aux N cas les plus probants (0 = tous)."),
    ] = 0,
    run_budget: Annotated[
        float, typer.Option(help="Plafond --max-budget-usd par run.")
    ] = 0.60,
    yes: Annotated[bool, typer.Option("--yes", help="Saute la confirmation.")] = False,
) -> None:
    """Valide un skill par replay contrôlé avec/sans (chiffre causal)."""
    import json as _json

    from ghost.replay import ReplayError
    from ghost.validate import (
        EST_COST_PER_RUN,
        aggregate,
        eligible_cases,
        rank_cases_by_motif,
        run_validation,
        skill_info,
        write_lift_frontmatter,
    )

    conn = connect(db)
    try:
        skill = skill_info(conn, skill_id)
        cases, counts = eligible_cases(conn, skill, match=match)
        if max_cases > 0 and len(cases) > max_cases:
            cases = rank_cases_by_motif(conn, skill, cases)[:max_cases]
            console.print(
                f"[dim]{max_cases} cas retenus (les plus probants par motif "
                f"d'erreur du skill)[/dim]"
            )
        if len(cases) < 3:
            console.print(
                f"[red]{len(cases)} cas éligibles (<3) — refus, conformément au "
                f"protocole.[/red] Comptes : {counts}"
            )
            raise typer.Exit(1)
        n_runs = len(cases) * 2 * runs
        estimate = n_runs * EST_COST_PER_RUN
        console.print(
            f"Replay [bold]{skill.slug}[/bold] · {len(cases)} cas · "
            f"{runs} runs/condition · ~{n_runs} runs · est. {estimate:.2f}$ "
            f"(plafond dur {max_cost:.2f}$)"
        )
        if not yes and not typer.confirm("Lancer ?", default=False):
            raise typer.Exit(0)
        report = run_validation(
            conn, skill, cases, max_cost_usd=max_cost, n_per_condition=runs,
            per_run_budget=run_budget,
            on_progress=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
        )
        for error in report.errors:
            console.print(f"[yellow]⚠ {error}[/yellow]")
        if report.stopped_on_budget:
            console.print("[yellow]arrêt sur plafond de coût — reprise possible "
                          "en relançant la même commande[/yellow]")

        lift = aggregate(conn, skill_id)
        console.print(f"\n[bold]— cas ({lift.n_cases} complets) —[/bold]")
        rows = conn.execute(
            "SELECT case_id, condition, metrics_json FROM replays "
            "WHERE skill_id = ? ORDER BY case_id, condition, run_idx",
            (skill_id,),
        ).fetchall()
        by_case: dict[str, dict[str, list[str]]] = {}
        for case_id, condition, mj in rows:
            m = _json.loads(mj)
            mark = "✓" if m.get("success") else "✗"
            by_case.setdefault(str(case_id), {}).setdefault(str(condition), []).append(
                f"{m.get('turns', 0)}{mark}"
            )
        for case_id, conds in by_case.items():
            console.print(f"  case {case_id}")
            for condition in ("sans", "avec"):
                vals = " / ".join(conds.get(condition, []))
                console.print(f"    {condition:<5} turns: {vals or '—'}")
        console.print(
            f"\n╭─ [bold]{lift.verdict.upper()}[/bold]\n"
            f"│  succès sans {lift.success_sans[0]}/{lift.success_sans[1]} → "
            f"avec {lift.success_avec[0]}/{lift.success_avec[1]}\n"
            f"│  n={lift.n_cases} cas · {lift.n_runs} runs · coût {lift.cost_usd:.2f}$\n"
            f"╰─ relance la même commande pour vérifier la reproductibilité"
        )
        for entry in lift.lifts:
            pct = f"{entry.delta_pct * 100:+.0f}%" if entry.delta_pct is not None else "—"
            coherent = "cohérent" if entry.consistent else "signes mixtes"
            console.print(
                f"   {entry.metric:<13} sans {entry.baseline_median:.0f} → "
                f"avec {entry.exposed_median:.0f}  ({pct}, {coherent})"
            )
        conn.execute(
            "UPDATE skills SET lift_json = ? WHERE id = ?",
            (
                _json.dumps(
                    {
                        "verdict": lift.verdict, "n_cases": lift.n_cases,
                        "n_runs": lift.n_runs, "cost_usd": lift.cost_usd,
                        "lifts": {
                            e.metric: e.delta_pct for e in lift.lifts
                        },
                    }
                ),
                skill_id,
            ),
        )
        conn.commit()
        if skill.source.exists():
            from ghost.deploy import convert_for_claude_code

            write_lift_frontmatter(skill.source, lift)
            n_propagated = 0
            for (target,) in conn.execute(
                "SELECT target_path FROM deployments WHERE skill_id = ?", (skill_id,)
            ):
                target_path = Path(str(target))
                if target_path.exists():
                    target_path.write_text(
                        convert_for_claude_code(
                            skill.source.read_text(encoding="utf-8")
                        ),
                        encoding="utf-8",
                    )
                    n_propagated += 1
            console.print(
                f"\nlift écrit dans {skill.source}, en base, et propagé à "
                f"{n_propagated} copie(s) déployée(s)"
            )
    except ReplayError as exc:
        console.print(f"[red]échec replay :[/red] {exc}")
        raise typer.Exit(1) from exc
    finally:
        conn.close()


@app.command()
def doctor(root: RootOpt = DEFAULT_ROOT, db: DbOpt = DEFAULT_DB) -> None:
    """Diagnostic d'installation — chaque ✗ dit quoi faire."""
    from ghost.doctor import run_doctor

    checks = run_doctor(root, db)
    for check in checks:
        mark = "[green]✓[/green]" if check.ok else "[red]✗[/red]"
        console.print(f"{mark} {check.label} — {check.detail}", highlight=False)
        if not check.ok and check.fix:
            console.print(f"    [yellow]→ {check.fix}[/yellow]", highlight=False)
    n_ok = sum(1 for c in checks if c.ok)
    console.print(f"\n{n_ok}/{len(checks)} vérifications OK")
    if n_ok < len(checks):
        raise typer.Exit(1)


@app.command()
def why(db: DbOpt = DEFAULT_DB) -> None:
    """Quels skills Ghost étaient injectables au dernier prompt, et pourquoi."""
    from ghost.manage import why_last

    conn = connect(db)
    try:
        session_id, injected = why_last(conn)
    finally:
        conn.close()
    if session_id is None:
        console.print("aucune session en base — lance `ghost ingest`.")
        return
    console.print(f"dernière session : {session_id[:8]}…")
    if not injected:
        console.print("aucun skill Ghost déployé n'était disponible dans cette session.")
        return
    console.print("skills Ghost injectables (déclenchés par leur description) :")
    for skill in injected:
        tag = f"#{skill.skill_id}" if skill.skill_id is not None else "?"
        console.print(f"  [bold]{skill.slug}[/bold] ({tag}) — {skill.description}",
                      highlight=False)
    console.print("\n`ghost disable <id>` pour qu'un skill ne soit plus injecté.")


@app.command()
def disable(skill_id: int, db: DbOpt = DEFAULT_DB) -> None:
    """Retire un skill déployé — plus jamais injecté (réactivable via enable)."""
    from ghost.manage import disable_skill

    conn = connect(db)
    try:
        removed, refused = disable_skill(conn, skill_id)
    finally:
        conn.close()
    console.print(f"skill {skill_id} désactivé · {len(removed)} fichier(s) retiré(s)")
    for path in removed:
        console.print(f"  - {path}")
    for path in refused:
        console.print(f"  [yellow]⚠ non retiré (hors ~/.claude/skills/) : {path}[/yellow]")
    console.print("`ghost enable <id>` pour réactiver.")


@app.command()
def enable(skill_id: int, db: DbOpt = DEFAULT_DB) -> None:
    """Réactive un skill désactivé (redéployable via ghost deploy)."""
    from ghost.manage import enable_skill

    conn = connect(db)
    try:
        enable_skill(conn, skill_id)
    finally:
        conn.close()
    console.print(f"skill {skill_id} réactivé — `ghost deploy` pour le repousser.")


@app.command()
def uninstall(
    db: DbOpt = DEFAULT_DB,
    yes: Annotated[bool, typer.Option("--yes", help="Sans confirmation.")] = False,
) -> None:
    """Retire tous les skills déployés par Ghost Brain (aucun hook installé)."""
    from ghost.manage import uninstall_skills

    if not yes and not typer.confirm(
        "Retirer tous les SKILL.md déployés par Ghost Brain ?", default=False
    ):
        raise typer.Exit(0)
    conn = connect(db)
    try:
        removed, refused = uninstall_skills(conn)
    finally:
        conn.close()
    console.print(f"{len(removed)} fichier(s) retiré(s).")
    for path in refused:
        console.print(f"  [yellow]⚠ non retiré (hors ~/.claude/skills/) : {path}[/yellow]")
    console.print(
        "Ghost Brain n'installe aucun hook dans settings.json — rien d'autre à "
        "nettoyer. La base ~/.ghost/ et le paquet restent (supprime-les à la main "
        "si tu le souhaites)."
    )


telemetry_app = typer.Typer(help="Télémétrie opt-in (off par défaut).")
app.add_typer(telemetry_app, name="telemetry")


@telemetry_app.command("status")
def telemetry_status() -> None:
    """État de la télémétrie."""
    from ghost.telemetry import TelemetryConfig

    cfg = TelemetryConfig.load()
    console.print(
        f"télémétrie : [bold]{'activée' if cfg.enabled else 'désactivée'}[/bold]\n"
        f"endpoint : {cfg.endpoint or '—'}\n"
        f"install_id : {cfg.install_id or '—'}"
    )


@telemetry_app.command("on")
def telemetry_on(endpoint: str) -> None:
    """Active la télémétrie vers <endpoint> (opt-in explicite)."""
    from ghost.telemetry import TelemetryConfig

    if not endpoint.startswith(("http://", "https://")):
        console.print("[red]endpoint invalide (http:// ou https:// requis)[/red]")
        raise typer.Exit(1)
    cfg = TelemetryConfig.load()
    cfg.enabled = True
    cfg.endpoint = endpoint
    cfg.save()
    console.print(
        f"télémétrie activée vers {endpoint}. `ghost telemetry preview` montre "
        "exactement ce qui serait envoyé ; `ghost telemetry off` à tout moment."
    )


@telemetry_app.command("off")
def telemetry_off() -> None:
    """Désactive la télémétrie."""
    from ghost.telemetry import TelemetryConfig

    cfg = TelemetryConfig.load()
    cfg.enabled = False
    cfg.save()
    console.print("télémétrie désactivée.")


@telemetry_app.command("preview")
def telemetry_preview(db: DbOpt = DEFAULT_DB) -> None:
    """Affiche le payload EXACT (comptes only, aucun texte brut) sans l'envoyer."""
    import json as _json

    from ghost import __version__
    from ghost.telemetry import TelemetryConfig, build_payload

    conn = connect(db)
    try:
        payload = build_payload(conn, TelemetryConfig.load(), __version__)
    finally:
        conn.close()
    console.print("[bold]payload télémétrie (jamais envoyé sans opt-in)[/bold] :")
    console.print(_json.dumps(payload.to_dict(), indent=2, ensure_ascii=False))
    console.print(
        "\nAucun prompt, code, chemin, nom de fichier ni contenu de skill n'y "
        "figure — uniquement des comptes agrégés."
    )


@telemetry_app.command("send")
def telemetry_send(db: DbOpt = DEFAULT_DB) -> None:
    """Envoie le payload maintenant (nécessite telemetry on)."""
    from ghost import __version__
    from ghost.telemetry import TelemetryConfig, build_payload, send

    cfg = TelemetryConfig.load()
    if not cfg.enabled or not cfg.endpoint:
        console.print("télémétrie désactivée — `ghost telemetry on <url>` d'abord.")
        raise typer.Exit(1)
    conn = connect(db)
    try:
        payload = build_payload(conn, cfg, __version__)
    finally:
        conn.close()
    ok, detail = send(payload, cfg.endpoint)
    console.print(f"{'envoyé' if ok else 'échec'} : {detail}")
    if not ok:
        raise typer.Exit(1)


def main() -> None:
    app()
