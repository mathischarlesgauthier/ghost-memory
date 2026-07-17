"""CLI Ghost Brain : `ghost ingest`, `ghost stats`."""

from __future__ import annotations

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
    summary = Summary(n_files=len(files), failures=[])
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
                assert summary.failures is not None
                summary.failures.append((str(source.path), result.error or "?"))
            else:
                summary.n_ingested += 1
                summary.n_events += result.n_events
                summary.n_skipped_lines += result.n_skipped_lines
    conn.close()

    for path, error in summary.failures or []:
        console.print(f"[yellow]⚠ fichier en échec :[/yellow] {path} — {error}")
    n_sessions = _count(db, "SELECT COUNT(*) FROM sessions")
    n_events_total = _count(db, "SELECT COUNT(*) FROM events")
    console.print(
        f"\n[bold]{n_sessions}[/bold] sessions · [bold]{n_events_total}[/bold] events en base — "
        f"ce run : {summary.n_ingested} fichiers ingérés, {summary.n_unchanged} inchangés, "
        f"{summary.n_failed} en échec, {summary.n_skipped_lines} lignes corrompues sautées"
    )


def _count(db: Path, sql: str) -> int:
    conn = connect(db)
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0
    finally:
        conn.close()


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


def main() -> None:
    app()
