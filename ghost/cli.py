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
def ingest(root: RootOpt = DEFAULT_ROOT, db: DbOpt = DEFAULT_DB) -> None:
    """Ingère ~/.claude/projects/**/*.jsonl (idempotent, streaming)."""
    conn = connect(db)
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


def main() -> None:
    app()
