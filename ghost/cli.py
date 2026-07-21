"""CLI Ghost Memory : `ghost ingest`, `ghost stats`, `ghost scan`, `ghost show`."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    import anthropic

import typer
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from ghost import ui
from ghost.db import DEFAULT_DB, connect
from ghost.ingest import DEFAULT_ROOT, Summary, ingest_all, scan_files

app = typer.Typer(add_completion=False, help="Ghost Memory — historique Claude Code → SQLite.")
console = ui.make_console()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    plain: Annotated[
        bool,
        typer.Option(
            "--plain",
            help="Sortie plate : sans couleur ni animation (scripts, CI).",
        ),
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", help="Affiche la version et sort.", is_eager=True),
    ] = False,
) -> None:
    """Ghost Memory — historique Claude Code → SQLite."""
    global console
    if plain:
        ui.force_plain(True)
        console = ui.make_console()
    if version:
        from ghost import __version__

        ui.version_card(console, __version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # `ghost` sans commande : accueil (une fois) puis l'aide.
        ui.maybe_welcome(console)
        console.print(ctx.get_help())
        raise typer.Exit()
    # Accueil affiché une seule fois, au premier lancement (jamais avant `welcome`,
    # jamais dans un pipe/CI, jamais ré-affiché ensuite).
    if ctx.invoked_subcommand != "welcome":
        ui.maybe_welcome(console)


@app.command()
def welcome() -> None:
    """Réaffiche l'écran d'accueil (logo + essence + premières actions)."""
    ui.welcome(console)

RootOpt = Annotated[Path, typer.Option(help="Racine des projets Claude Code.")]
DbOpt = Annotated[Path, typer.Option(help="Chemin de la base SQLite.")]


def _resolve_skill_id(conn: sqlite3.Connection, token: str) -> int:
    """Résout un token (id skill / id candidat / slug) en id de SKILL, ou sort
    proprement avec un message actionnable."""
    from ghost.resolve import ResolveError, resolve_skill

    try:
        r = resolve_skill(conn, token)
    except ResolveError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if r.note:
        console.print(f"[dim]{r.note}[/dim]")
    return r.id


def _resolve_candidate_id(conn: sqlite3.Connection, token: str) -> int:
    """Résout un token en id de CANDIDAT, ou sort proprement."""
    from ghost.resolve import ResolveError, resolve_candidate

    try:
        r = resolve_candidate(conn, token)
    except ResolveError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if r.note:
        console.print(f"[dim]{r.note}[/dim]")
    return r.id


def _anthropic_client() -> anthropic.Anthropic:
    """Client Anthropic authentifié par la clé LOCALE : ~/.ghost/api_key (écrit
    par `ghost init`, source de vérité) puis ANTHROPIC_API_KEY. Sort proprement
    si aucune — jamais le traceback SDK « Could not resolve authentication
    method »."""
    import anthropic as _anthropic

    from ghost.onboard import ApiKeyMissing, resolve_api_key

    try:
        key = resolve_api_key()
    except ApiKeyMissing as exc:
        ui.fail(
            console,
            "aucune clé Anthropic locale",
            "lance `ghost init` (ou exporte ANTHROPIC_API_KEY)",
        )
        raise typer.Exit(1) from exc
    return _anthropic.Anthropic(api_key=key)


@app.command()
def init(
    root: RootOpt = DEFAULT_ROOT,
    db: DbOpt = DEFAULT_DB,
    api_key: Annotated[
        str | None, typer.Option(help="Clé Anthropic (sinon demandée/opt-in).")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Non-interactif : ne demande rien.")
    ] = False,
    no_scan: Annotated[
        bool, typer.Option("--no-scan", help="Ne lance pas le premier scan.")
    ] = False,
) -> None:
    """Onboarding : PATH, clé API, détection Claude Code, premier scan guidé.

    Conçu pour ne JAMAIS planter sur une machine vierge : chaque étape absente
    donne une consigne, pas une trace.
    """
    import os as _os

    from ghost.onboard import (
        API_KEY_FILE,
        detect_shell_rc,
        ghost_on_path,
        history_status,
        ping_api_key,
        write_api_key,
    )

    console.print("[bold]Ghost Memory — installation guidée[/bold]\n")

    # 1. PATH
    if ghost_on_path():
        console.print("[green]✓[/green] `ghost` est sur le PATH.")
    else:
        console.print(
            "[yellow]•[/yellow] `ghost` n'est pas sur le PATH.\n"
            "    → lance [bold]uv tool update-shell[/bold] puis rouvre ton terminal "
            f"(ou ajoute ~/.local/bin au PATH dans {detect_shell_rc()})."
        )

    # 2. Clé API (opt-in, jamais commitée, chmod 600)
    key: str | None = api_key
    if key is None and API_KEY_FILE.exists():
        console.print("[green]✓[/green] clé API déjà enregistrée (~/.ghost/api_key).")
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    if key is None:
        env_key = _os.environ.get("ANTHROPIC_API_KEY")
        if env_key:
            key = env_key
        elif not yes:
            key = typer.prompt(
                "Colle ta clé Anthropic (sk-ant-…)", hide_input=True
            ).strip()
    if key:
        ok, msg = ping_api_key(key)
        if ok:
            write_api_key(key)
            console.print(f"[green]✓[/green] {msg} — enregistrée (chmod 600).")
        else:
            console.print(
                f"[red]✗[/red] {msg}\n    Récupère une clé et relance `ghost init`."
            )
    else:
        console.print(
            "[yellow]•[/yellow] pas de clé API — `ingest`/`scan` marchent sans, mais "
            "`distill`/`validate`/`bench` en auront besoin. Relance `ghost init` "
            "quand tu l'auras."
        )

    # 3. Claude Code / historique
    hist = history_status(root)
    if hist.projects_exist and hist.n_files:
        console.print(
            f"[green]✓[/green] historique Claude Code : {hist.n_files} session(s)."
        )
    elif hist.projects_exist:
        console.print(
            f"[yellow]•[/yellow] {root} existe mais est vide — code un peu avec "
            "Claude Code, puis relance `ghost init`."
        )
    else:
        console.print(
            f"[yellow]•[/yellow] aucun historique Claude Code ({root} absent).\n"
            "    → installe Claude Code (code.claude.com) et code un peu : Ghost "
            "apprend de ton historique."
        )

    # 4. Premier scan guidé — l'activation, c'est voir ses candidats.
    if no_scan or not (hist.projects_exist and hist.n_files):
        console.print(
            "\nProchaine étape quand tu auras de l'historique : "
            "[bold]ghost run[/bold] (ingest + scan + distille)."
        )
        return
    console.print("\n[bold]Premier scan…[/bold]")
    from ghost.ingest import ingest_all, scan_files
    from ghost.scan import run_scan

    conn = connect(db)
    try:
        files = scan_files(root)
        for _source, _result in ingest_all(conn, root):
            pass
        if conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0:
            console.print(
                "historique présent mais illisible — `ghost doctor` pour diagnostiquer."
            )
            return
        merged = run_scan(conn)
        console.print(
            f"[green]✓[/green] {len(files)} fichier(s) ingéré(s), "
            f"[bold]{len(merged)}[/bold] candidat(s) détecté(s).\n"
        )
        table = Table(title="Tes candidats (top 10)")
        for col in ("id", "kind", "signature", "score"):
            table.add_column(col)
        for cid, kind, sig, score in conn.execute(
            "SELECT id, kind, signature, score FROM candidates "
            "WHERE status = 'new' ORDER BY score DESC LIMIT 10"
        ):
            table.add_row(str(cid), str(kind), str(sig)[:60], f"{score:.1f}")
        console.print(table)
        console.print(
            "\nRegarde-en un : [bold]ghost show <id>[/bold] · distille : "
            "[bold]ghost distill <id>[/bold] · ou tout d'un coup : [bold]ghost run[/bold]."
        )
    finally:
        conn.close()


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
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    with ui.step(console, f"scan de {n_sessions} sessions — détection des cicatrices…"):
        merged = run_scan(conn)
    by_kind = {k: sum(1 for c in merged if c.kind == k) for k in
               ("FAILURE_LOOP", "HUMAN_OVERRIDE", "REPEATED_SEQUENCE")}
    ui.summary(
        console,
        "scan",
        f"{len(merged)} candidats · {by_kind['FAILURE_LOOP']} boucles d'échec · "
        f"{by_kind['HUMAN_OVERRIDE']} corrections · "
        f"{by_kind['REPEATED_SEQUENCE']} répétitions",
    )
    rows = conn.execute(
        "SELECT id, kind, signature, score, n_occ, n_sessions, evidence_json, status "
        "FROM candidates ORDER BY score DESC LIMIT 15"
    ).fetchall()
    blocks: list[str] = []
    for cid, kind, signature, score, n_occ, n_sessions, evidence_json, status in rows:
        label = signature
        if kind == "HUMAN_OVERRIDE":
            evidence = _json.loads(evidence_json)
            excerpt = str(evidence[0].get("meta", {}).get("excerpt", "")) if evidence else ""
            label = f"{signature}  «{excerpt[:70]}»"
        flag = "" if status == "new" else f" [{status}]"
        blocks.append(
            f"  [bold]{cid:4d}[/bold]. [{kind}] {label[:96]}\n"
            f"        [grey58]score {score:.1f} · {n_occ} occ · {n_sessions} sess{flag}[/grey58]"
        )
    console.print()
    ui.reveal(console, blocks)  # léger stagger : « il réfléchit », plafonné
    total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    console.print(
        f"\n  [grey58]top {min(15, int(total))} affichés · "
        f"{total} candidats en table[/grey58]"
    )
    conn.close()


@app.command()
def show(
    candidate: Annotated[str, typer.Argument(help="id candidat, id skill, ou slug.")],
    db: DbOpt = DEFAULT_DB,
    max_occurrences: Annotated[int, typer.Option(help="Occurrences affichées.")] = 5,
) -> None:
    """Dump l'evidence d'un candidat, events bruts inclus."""
    import json as _json

    conn = connect(db)
    candidate_id = _resolve_candidate_id(conn, candidate)
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
    candidate: Annotated[str, typer.Argument(help="id candidat, id skill, ou slug.")],
    db: DbOpt = DEFAULT_DB,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Affiche le prompt et la trace, n'appelle pas.")
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Redistille même si un skill existe déjà (l'ancien est désactivé).",
        ),
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
    from ghost.resolve import skills_for_candidate
    from ghost.trace import build_trace

    conn = connect(db)
    superseded = 0
    try:
        candidate_id = _resolve_candidate_id(conn, candidate)
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
        existing = skills_for_candidate(conn, candidate_id)
        if existing and not force:
            listing = ", ".join(f"{sid}:{slug}" for sid, slug in existing)
            console.print(
                f"[yellow]candidat {candidate_id} a déjà un skill actif : "
                f"{listing}.[/yellow]\n"
                "Re-distiller créerait un doublon. Relance avec [bold]--force[/bold] "
                "pour redistiller (l'ancien sera désactivé), ou édite le SKILL.md "
                "existant directement."
            )
            raise typer.Exit(1)
        client = _anthropic_client()  # clé locale (~/.ghost/api_key) puis env
        with ui.step(
            console,
            f"distillation du candidat {candidate_id} — "
            "trace → modèle → auto-critique…",
        ):
            result = run_distill(
                conn, candidate_id, caller=default_caller(client)
            )
        if result.verdict == "SKILL":
            # Dédup : ne garder actif que le skill le plus récent du candidat.
            keep_id = conn.execute(
                "SELECT MAX(id) FROM skills WHERE candidate_id = ? AND verdict = 'SKILL'",
                (candidate_id,),
            ).fetchone()[0]
            superseded = conn.execute(
                "UPDATE skills SET disabled = 1 WHERE candidate_id = ? "
                "AND verdict = 'SKILL' AND id <> ? AND disabled = 0",
                (candidate_id, keep_id),
            ).rowcount
            conn.commit()
    except (DistillError, RedactionError, ValueError) as exc:
        ui.fail(console, f"échec distillation : {exc}", "inspecte le candidat : `ghost show <id>`")
        raise typer.Exit(1) from exc
    except _anthropic.APIError as exc:
        ui.fail(
            console,
            f"erreur API : {type(exc).__name__}: {exc}",
            "vérifie ta clé (`ghost init`) et ta connexion",
        )
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    ui.summary(
        console,
        f"distill · {result.verdict}",
        f"{result.tokens_in} tokens in · {result.tokens_out} out · "
        f"{result.cost_usd:.3f}$ · redactions {result.trace.redactions or 'aucune'}",
        style=ui.OK if result.verdict == "SKILL" else ui.MUTED,
    )
    if result.verdict == "SKIP":
        console.print(f"[grey58]raison : {result.skip_reason}[/grey58]")
        return
    if superseded:
        console.print(
            f"[dim]dédup : {superseded} ancien(s) skill(s) du même candidat "
            "désactivé(s) — plus de doublon.[/dim]"
        )
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


@app.command()
def create(
    url: Annotated[
        str,
        typer.Argument(
            metavar="GITHUB_URL",
            help="Lien vers un skill GitHub (page /blob/… ou lien raw).",
        ),
    ],
    db: DbOpt = DEFAULT_DB,
    yes: Annotated[bool, typer.Option("--yes", help="Saute la confirmation.")] = False,
) -> None:
    """Importe un skill GitHub, le normalise, et l'ajoute à tes skills locaux."""
    import anthropic as _anthropic

    from ghost.create import (
        CreateError,
        base_slug,
        create_local_skill,
        fetch_skill_md,
        github_license_for_url,
        normalize_skill,
        render_skill_md,
        source_repo_from_url,
    )
    from ghost.distill import DEFAULT_SKILLS_DIR, default_caller

    # Clé Anthropic LOCALE (fichier `ghost init` puis env) — résolue avant tout
    # accès réseau pour échouer vite et clairement.
    caller = default_caller(_anthropic_client())

    try:
        with ui.step(console, f"récupération de {url}…"):
            raw = fetch_skill_md(url)
    except CreateError as exc:
        ui.fail(
            console,
            f"lien invalide : {exc}",
            "attendu un fichier de skill sur github.com/…/blob/… ou raw.githubusercontent.com",
        )
        raise typer.Exit(1) from exc
    if not raw.strip():
        ui.fail(console, "le fichier est vide", "vérifie que le lien pointe bien vers un SKILL.md")
        raise typer.Exit(1)

    source = source_repo_from_url(url)
    try:
        with ui.step(console, "normalisation — distillateur (Sonnet 5)…"):
            lic = github_license_for_url(url)
            norm = normalize_skill(raw, caller=caller)
    except CreateError as exc:
        ui.fail(
            console,
            f"normalisation impossible : {exc}",
            "réessaie, ou vérifie ta clé (`ghost init`)",
        )
        raise typer.Exit(1) from exc
    except _anthropic.APIError as exc:
        ui.fail(
            console,
            f"erreur API : {type(exc).__name__}: {exc}",
            "vérifie ta clé (`ghost init`) et ta connexion",
        )
        raise typer.Exit(1) from exc

    ui.summary(
        console,
        f"create · {norm.verdict}",
        f"{norm.tokens_in} tokens in · {norm.tokens_out} out · {norm.cost_usd:.3f}$",
        style=ui.OK if norm.verdict == "SKILL" else ui.MUTED,
    )
    if norm.verdict == "SKIP":
        reason = norm.skip_reason or "contenu générique / pas un skill"
        console.print(ui.verdict("SKIP"))
        console.print(f"[grey58]raison : {reason}[/grey58]")
        console.print("[grey58]rien écrit — ce lien n'est pas un skill exploitable.[/grey58]")
        raise typer.Exit(0)

    skill_md = render_skill_md(norm, source=source, license=lic, raw_md=raw)
    console.print(ui.verdict("SKILL"))
    console.print(f"  [bold]{base_slug(norm.name)}[/bold]")
    console.print(f"  signature de tâche : [grey58]{norm.signature}[/grey58]")
    if not lic:
        console.print("  [yellow]⚠ license non détectée sur le dépôt → « unknown »[/yellow]")
    console.print("\n[dim]— frontmatter généré (le corps d'origine est conservé) —[/dim]")
    for line in skill_md.splitlines()[:12]:
        # markup=False : les listes `tags: [a, b]` ne sont pas lues comme du balisage rich.
        console.print(f"  {line}", markup=False, highlight=False)

    if not yes and not typer.confirm(
        "\nAjouter ce skill à tes skills locaux ?", default=False
    ):
        raise typer.Exit(0)

    conn = connect(db)
    try:
        created = create_local_skill(
            conn, url=url, norm=norm, skill_md=skill_md, skills_dir=DEFAULT_SKILLS_DIR
        )
        conn.commit()
    finally:
        conn.close()
    verb = "ré-importé (nouvelle version, ancien désactivé)" if created.reimport else "ajouté"
    ui.ok(console, f"skill {verb} : {created.slug}")
    console.print(f"  écrit : {created.path}")
    console.print(
        "  visible dans [bold]ghost skills[/bold] · déployable via "
        "[bold]ghost deploy[/bold] · publiable via [bold]ghost publish[/bold]"
    )


def _set_status(db: Path, candidate: str, status: str) -> None:
    conn = connect(db)
    try:
        candidate_id = _resolve_candidate_id(conn, candidate)
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
def keep(
    candidate: Annotated[str, typer.Argument(help="id candidat, id skill, ou slug.")],
    db: DbOpt = DEFAULT_DB,
) -> None:
    """Valide un candidat (déployable via ghost deploy)."""
    _set_status(db, candidate, "kept")


@app.command()
def reject(
    candidate: Annotated[str, typer.Argument(help="id candidat, id skill, ou slug.")],
    db: DbOpt = DEFAULT_DB,
) -> None:
    """Rejette un candidat (survit aux re-scans, jamais re-proposé)."""
    _set_status(db, candidate, "rejected")


@app.command()
def skills(db: DbOpt = DEFAULT_DB) -> None:
    """Liste les skills distillés : verdict, coût, statut, déploiement, doublons."""
    conn = connect(db)
    rows = conn.execute(
        """
        SELECT sk.id, sk.candidate_id, COALESCE(sk.slug, '—'), sk.verdict,
               sk.low_value, sk.disabled, sk.cost_usd, c.status,
               (SELECT COUNT(*) FROM deployments d WHERE d.skill_id = sk.id)
        FROM skills sk LEFT JOIN candidates c ON c.id = sk.candidate_id
        ORDER BY sk.candidate_id, sk.id
        """
    ).fetchall()
    if not rows:
        console.print(
            "aucun skill distillé pour l'instant — lance `ghost run` (ou "
            "`ghost distill <candidat>`) après un `ghost scan`."
        )
        conn.close()
        return
    # Doublon = candidat ayant >1 skill SKILL ACTIF (non désactivé).
    active_by_cand: dict[int, int] = {}
    for _sid, cid, _slug, verdict, _lv, disabled, _c, _st, _nd in rows:
        if verdict == "SKILL" and not disabled:
            active_by_cand[int(cid)] = active_by_cand.get(int(cid), 0) + 1
    dup_cands = {cid for cid, n in active_by_cand.items() if n > 1}
    table = Table(title="Skills distillés")
    for col in ("id", "candidat", "slug", "verdict", "coût", "statut", "déployé"):
        table.add_column(col)
    for sid, cid, slug, verdict, low_value, disabled, cost, status, n_dep in rows:
        flags = ""
        if low_value:
            flags += " ⚠low_value"
        if disabled:
            flags += " (désactivé)"
        if int(cid) in dup_cands and verdict == "SKILL" and not disabled:
            flags += " ⚠DOUBLON"
        table.add_row(
            str(sid), str(cid), str(slug), f"{verdict}{flags}", f"{cost:.3f}$",
            str(status or "?"), "oui" if n_dep else "non",
            style="dim" if disabled else "",
        )
    console.print(table)
    if dup_cands:
        listing = ", ".join(str(c) for c in sorted(dup_cands))
        console.print(
            f"\n[yellow]⚠ {len(dup_cands)} candidat(s) avec doublon : {listing}. "
            "`ghost distill <candidat> --force` régénère et désactive l'ancien ; "
            "ou `ghost disable <id>` retire manuellement un doublon.[/yellow]"
        )
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
    from ghost.distill import default_caller
    from ghost.pipeline import run_pipeline

    client = _anthropic_client()  # clé locale (~/.ghost/api_key) puis env
    conn = connect(db)
    try:
        with ui.step(console, "ghost run — ingest → scan → distillation sous budget…"):
            report = run_pipeline(
                conn, caller=default_caller(client),
                root=root, budget_usd=budget, top_n=top,
            )
    finally:
        conn.close()

    ui.summary(
        console,
        "ghost run",
        f"{report.n_files_ingested} fichiers ingérés ({report.n_files_unchanged} inchangés) · "
        f"{report.n_candidates_total} candidats · "
        f"dépense {report.spent_usd:.2f}$ / {budget:.2f}$",
    )
    console.print()
    blocks: list[str] = []
    for item in report.items:
        line = f"  {item.candidate_id:5d} [{item.kind}] {item.signature[:60]}"
        if item.outcome == "SKILL":
            blocks.append(
                f"{line}\n        → [green]SKILL[/green] {item.slug} ({item.cost_usd:.3f}$)"
            )
        elif item.outcome == "SKIP":
            blocks.append(f"{line}\n        → [grey58]SKIP[/grey58] ({item.cost_usd:.3f}$)")
        elif item.outcome == "BUDGET":
            blocks.append(f"{line}\n        → [yellow]non distillé (budget épuisé)[/yellow]")
        else:
            blocks.append(f"{line}\n        → [red3]ERREUR[/red3] {item.error}")
    ui.reveal(console, blocks)
    console.print(
        "\n[grey58]Triage : `ghost skills` puis `ghost keep <candidat>` "
        "et `ghost deploy`.[/grey58]"
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
    skill_ref: Annotated[
        str, typer.Argument(metavar="SKILL", help="id skill, id candidat, ou slug.")
    ],
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
    allow_underpowered: Annotated[
        bool,
        typer.Option(
            "--allow-underpowered",
            help="Mode debug : rejoue même sous le seuil de 3 cas. Le résultat "
            "est marqué NON STATISTIQUEMENT VALIDE et n'est écrit ni dans le "
            "SKILL.md ni dans lift_json.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Saute la confirmation.")] = False,
) -> None:
    """Valide un skill par replay contrôlé avec/sans (chiffre causal)."""
    import json as _json

    from ghost.replay import ReplayError
    from ghost.validate import (
        EST_COST_PER_RUN,
        MIN_CASES,
        aggregate,
        eligible_cases,
        rank_cases_by_motif,
        run_validation,
        skill_info,
        write_lift_frontmatter,
    )

    conn = connect(db)
    try:
        skill_id = _resolve_skill_id(conn, skill_ref)
        skill = skill_info(conn, skill_id)
        cases, counts = eligible_cases(conn, skill, match=match)
        if max_cases > 0 and len(cases) > max_cases:
            cases = rank_cases_by_motif(conn, skill, cases)[:max_cases]
            console.print(
                f"[dim]{max_cases} cas retenus (les plus probants par motif "
                f"d'erreur du skill)[/dim]"
            )
        underpowered = len(cases) < MIN_CASES
        if underpowered and not allow_underpowered:
            console.print(
                f"[red]{len(cases)} cas éligibles (<{MIN_CASES}) — refus, conformément au "
                f"protocole.[/red] Comptes : {counts}\n"
                "[dim]Debug : --allow-underpowered lance quand même la mécanique "
                "(résultat non statistiquement valide).[/dim]"
            )
            raise typer.Exit(1)
        if not cases:
            console.print(
                f"[red]0 cas éligible — rien à rejouer, même avec "
                f"--allow-underpowered.[/red] Comptes : {counts}"
            )
            raise typer.Exit(1)
        if underpowered:
            console.print(
                f"[bold yellow]⚠ MODE DEBUG — {len(cases)} cas (<{MIN_CASES}) : "
                "résultat NON STATISTIQUEMENT VALIDE.[/bold yellow]\n"
                "[yellow]Ce run sert uniquement à vérifier la plomberie (baseline "
                "reconstruite, agent lancé sans/avec skill, comparaison) ; rien ne "
                "sera écrit dans le SKILL.md ni dans lift_json.[/yellow]"
            )
        n_runs = len(cases) * 2 * runs
        estimate = n_runs * EST_COST_PER_RUN
        console.print(
            f"Replay [bold]{skill.slug}[/bold] · {len(cases)} cas · "
            f"{runs} runs/condition · ~{n_runs} runs · est. {estimate:.2f}$ "
            f"(plafond dur {max_cost:.2f}$)"
        )
        if not yes and not typer.confirm("Lancer ?", default=False):
            raise typer.Exit(0)
        with ui.step(
            console, f"replay {skill.slug} — ~{n_runs} runs (avec / sans)…"
        ) as set_status:
            report = run_validation(
                conn, skill, cases, max_cost_usd=max_cost, n_per_condition=runs,
                per_run_budget=run_budget,
                on_progress=set_status,
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
        debug_tag = (
            f"[bold yellow]NON STATISTIQUEMENT VALIDE (debug, n<{MIN_CASES})[/bold yellow] · "
            if underpowered
            else ""
        )
        console.print(
            f"\n╭─ {debug_tag}[bold]{lift.verdict.upper()}[/bold]\n"
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
        if underpowered:
            console.print(
                "\n[yellow]mode debug : lift NON STATISTIQUEMENT VALIDE — "
                "non écrit dans le SKILL.md ni dans lift_json (les runs restent "
                "en base et compteront si tu relances avec assez de cas).[/yellow]"
            )
            return
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
def bench(
    skill_ref: Annotated[
        str, typer.Argument(metavar="SKILL", help="id skill, id candidat, ou slug.")
    ],
    db: DbOpt = DEFAULT_DB,
    max_cost: Annotated[float, typer.Option(help="Plafond de dépense ($).")] = 6.0,
    runs: Annotated[int, typer.Option(help="Runs par condition et par banc (≥3).")] = 3,
    run_budget: Annotated[
        float, typer.Option(help="Plafond --max-budget-usd par run.")
    ] = 0.40,
    yes: Annotated[bool, typer.Option("--yes", help="Saute la confirmation.")] = False,
) -> None:
    """Mesure le lift sur des micro-benchmarks SYNTHÉTIQUES (pas du vrai replay).

    Baseline auto-contenue qui réussit vraiment → un lift veut dire quelque chose.
    Le corpus n'ayant aucun cas court rejouable (cf. Lot A), c'est la voie de
    mesure retenue. Chaque run réussit ssi le grader déterministe du banc passe.
    """
    from ghost.benchmarks import benches_for, run_bench_validation
    from ghost.replay import ReplayError
    from ghost.validate import skill_info

    conn = connect(db)
    try:
        skill_id = _resolve_skill_id(conn, skill_ref)
        skill = skill_info(conn, skill_id)
        benches = benches_for(skill.slug)
        if not benches:
            console.print(
                f"[yellow]aucun micro-benchmark ne cible « {skill.slug} ».[/yellow]\n"
                "Les bancs sont écrits par scar ciblé (voir ghost/benchmarks.py). "
                "Ajoute-en un dont target_skills contient ce slug."
            )
            raise typer.Exit(1)
        n_runs = len(benches) * 2 * runs
        console.print(
            f"[dim]SYNTHÉTIQUE — pas du vrai replay.[/dim] Bancs pour "
            f"[bold]{skill.slug}[/bold] : {', '.join(b.slug for b in benches)}\n"
            f"{len(benches)} banc(s) · {runs} runs/condition · ~{n_runs} runs · "
            f"plafond dur {max_cost:.2f}$ (budget {run_budget:.2f}$/run)"
        )
        if not yes and not typer.confirm("Lancer ?", default=False):
            raise typer.Exit(0)
        lift = run_bench_validation(
            conn, skill, benches, max_cost_usd=max_cost, n_per_condition=runs,
            per_run_budget=run_budget,
            on_progress=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
        )
        console.print(
            f"\n╭─ [bold]{lift.verdict.upper()}[/bold]  [dim](synthétique)[/dim]\n"
            f"│  succès sans {lift.success_sans[0]}/{lift.success_sans[1]} → "
            f"avec {lift.success_avec[0]}/{lift.success_avec[1]}"
            + (
                f"  · incomplets(coupés) sans {lift.incomplete_sans} / "
                f"avec {lift.incomplete_avec}"
                if (lift.incomplete_sans or lift.incomplete_avec)
                else ""
            )
            + f"\n│  n={lift.n_cases} bancs · {lift.n_runs} runs · "
            f"coût {lift.cost_usd:.2f}$\n"
            f"╰─ relance la même commande pour vérifier la reproductibilité"
        )
        for entry in lift.lifts:
            pct = f"{entry.delta_pct * 100:+.0f}%" if entry.delta_pct is not None else "—"
            coherent = "cohérent" if entry.consistent else "signes mixtes"
            console.print(
                f"   {entry.metric:<13} sans {entry.baseline_median:.0f} → "
                f"avec {entry.exposed_median:.0f}  ({pct}, {coherent})"
            )
    except ReplayError as exc:
        console.print(f"[red]échec :[/red] {exc}")
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
def disable(
    skill_ref: Annotated[
        str, typer.Argument(metavar="SKILL", help="id skill, id candidat, ou slug.")
    ],
    db: DbOpt = DEFAULT_DB,
) -> None:
    """Retire un skill déployé — plus jamais injecté (réactivable via enable)."""
    from ghost.manage import disable_skill

    conn = connect(db)
    try:
        skill_id = _resolve_skill_id(conn, skill_ref)
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
def enable(
    skill_ref: Annotated[
        str, typer.Argument(metavar="SKILL", help="id skill, id candidat, ou slug.")
    ],
    db: DbOpt = DEFAULT_DB,
) -> None:
    """Réactive un skill désactivé (redéployable via ghost deploy)."""
    from ghost.manage import enable_skill

    conn = connect(db)
    try:
        skill_id = _resolve_skill_id(conn, skill_ref)
        enable_skill(conn, skill_id)
    finally:
        conn.close()
    console.print(f"skill {skill_id} réactivé — `ghost deploy` pour le repousser.")


@app.command()
def uninstall(
    db: DbOpt = DEFAULT_DB,
    yes: Annotated[bool, typer.Option("--yes", help="Sans confirmation.")] = False,
) -> None:
    """Retire tous les skills déployés par Ghost Memory (aucun hook installé)."""
    from ghost.manage import uninstall_skills

    if not yes and not typer.confirm(
        "Retirer tous les SKILL.md déployés par Ghost Memory ?", default=False
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
        "Ghost Memory n'installe aucun hook dans settings.json — rien d'autre à "
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


ApiUrlOpt = Annotated[
    str | None, typer.Option("--api-url", help="Base API Ghost (sinon GHOST_API_URL).")
]


@app.command()
def login(
    api_url: ApiUrlOpt = None,
    token: Annotated[
        str | None,
        typer.Option("--token", help="Adopte un jeton existant (créé sur le web)."),
    ] = None,
) -> None:
    """Se connecte au réseau Ghost (device flow). Stocke un jeton Ghost — jamais
    ta clé Anthropic. `--token` adopte un jeton déjà créé sur le site."""
    import time
    import webbrowser
    from contextlib import suppress

    from ghost.network import NetworkError, api_base, device_login, save_token

    if token:
        path = save_token(token)
        console.print(f"[green]✓ jeton enregistré[/green] ({path}, chmod 600).")
        return

    base = api_url or api_base()

    def prompt(uri: str, code: str) -> None:
        console.print(
            f"Ouvre [bold]{uri}[/bold] et entre le code : [bold]{code}[/bold]"
        )
        with suppress(Exception):
            webbrowser.open(uri)

    try:
        token = device_login(base=base, on_prompt=prompt, poll=time.sleep)
    except NetworkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    path = save_token(token)
    console.print(f"[green]✓ connecté[/green] — jeton enregistré ({path}, chmod 600).")


@app.command()
def retrieve(
    signature: Annotated[
        str | None,
        typer.Argument(
            help="Signature de tâche. Défaut : calculée depuis ta dernière session locale."
        ),
    ] = None,
    db: DbOpt = DEFAULT_DB,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Calcule la signature depuis cette session locale."),
    ] = None,
    limit: Annotated[int, typer.Option(help="Nombre de skills remontés.")] = 10,
    api_url: ApiUrlOpt = None,
) -> None:
    """Cherche dans la mémoire collective les skills d'une classe de tâche.

    Métadonnées seules, classées par lift mesuré (les seeds non mesurés suivent).
    Sans argument, la signature est déduite de ta dernière session locale."""
    from ghost.network import NetworkError, api_base, load_token
    from ghost.network import retrieve as net_retrieve
    from ghost.signature import task_signature

    token = load_token()
    if not token:
        ui.fail(console, "pas connecté au réseau", "connecte-toi : `ghost login`")
        raise typer.Exit(1)

    sig = signature
    if sig is None:
        conn = connect(db)
        try:
            sid = session
            if sid is None:
                row = conn.execute(
                    "SELECT id FROM sessions "
                    "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    ui.fail(
                        console,
                        "aucune session locale pour déduire une signature",
                        "ingère ton historique (`ghost ingest`) ou passe une signature en argument",
                    )
                    raise typer.Exit(1)
                sid = str(row[0])
            sig = task_signature(conn, sid)
        finally:
            conn.close()
        console.print(f"[grey58]signature : {sig}[/grey58]")

    base = api_url or api_base()
    try:
        with ui.step(console, "interrogation de la mémoire collective…"):
            data = net_retrieve(sig, token, limit=limit, base=base)
    except NetworkError as exc:
        ui.fail(console, str(exc), "réessaie quand le réseau répond")
        raise typer.Exit(1) from exc

    raw = data.get("skills")
    skills = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
    if not skills:
        msg = data.get("message") or "rien pour cette signature — la mémoire se remplit."
        console.print(f"\n[grey58]{msg}[/grey58]")
        return

    ui.summary(console, "retrieve", f"{len(skills)} skill(s) pour cette classe de tâche")
    table = Table(show_header=True, header_style="bold")
    table.add_column("skill")
    table.add_column("lift", justify="right")
    table.add_column("statut")
    table.add_column("source / auteur")
    for s in skills:
        lift = s.get("mean_lift")
        lift_txt = (
            f"{lift * 100:+.0f}%"
            if isinstance(lift, int | float)
            else "[grey58]non mesuré[/grey58]"
        )
        status_txt = (
            "[green]verified[/green]"
            if s.get("status") == "verified"
            else "[grey58]unverified[/grey58]"
        )
        if s.get("seed"):
            status_txt += " [#7fb0ff]·seed[/#7fb0ff]"
        src = str(s.get("source") or s.get("author") or "—")
        table.add_row(str(s.get("slug")), lift_txt, status_txt, src)
    console.print(table)
    console.print(
        "\n[grey58]classé par lift mesuré ; les non mesurés (seeds) suivent · "
        "corps via déblocage (Pro).[/grey58]"
    )


@app.command()
def logout() -> None:
    """Supprime le jeton Ghost local."""
    from ghost.network import clear_token

    clear_token()
    console.print("déconnecté — jeton supprimé.")


@app.command()
def upgrade(
    tier: Annotated[str, typer.Argument(help="pro | team | scale")],
    api_url: ApiUrlOpt = None,
) -> None:
    """Ouvre le Checkout Stripe pour passer à un palier payant (nécessite login)."""
    import webbrowser
    from contextlib import suppress

    from ghost.network import NetworkError, checkout_url, load_token

    token = load_token()
    if not token:
        console.print("connecte-toi d'abord : [bold]ghost login[/bold]")
        raise typer.Exit(1)
    try:
        url = checkout_url(tier, token, base=api_url or None)
    except NetworkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"Ouvre pour payer (test) : [bold]{url}[/bold]")
    with suppress(Exception):
        webbrowser.open(url)


@app.command()
def usage() -> None:
    """Consommation du cycle : palier, déblocages utilisés / quota, reset."""
    from ghost import account as acct

    raise typer.Exit(acct.run(acct.usage))


@app.command()
def unlocked() -> None:
    """Skills communautaires débloqués ce cycle, classés par lift mesuré."""
    from ghost import account as acct

    raise typer.Exit(acct.run(acct.unlocked))


@app.command()
def earnings() -> None:
    """Balance de rémunération (50% du pool, payé au lift x adoption)."""
    from ghost import account as acct

    raise typer.Exit(acct.run(acct.earnings))


@app.command()
def account() -> None:
    """Tableau de bord du compte : palier, usage, earnings, profil."""
    from ghost import account as acct

    raise typer.Exit(acct.run(acct.account))


@app.command()
def history() -> None:
    """Historique des versements passés."""
    from ghost import account as acct

    raise typer.Exit(acct.run(acct.history))


@app.command()
def whoami() -> None:
    """Palier + email/handle courant (debug rapide)."""
    from ghost import account as acct

    raise typer.Exit(acct.run(acct.whoami))


@app.command(name="payout-setup")
def payout_setup() -> None:
    """Active les versements — ouvre une page sécurisée (aucune donnée bancaire
    dans le terminal). Optionnel : nécessaire seulement pour retirer."""
    from ghost import account as acct

    raise typer.Exit(acct.run(acct.payout_setup))


@app.command()
def publish(
    skill_ref: Annotated[
        str, typer.Argument(metavar="SKILL", help="id skill, id candidat, ou slug.")
    ],
    db: DbOpt = DEFAULT_DB,
    public: Annotated[
        bool, typer.Option("--public", help="Publier en PUBLIC (défaut : privé).")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Saute la confirmation.")] = False,
) -> None:
    """Publie un skill vers la mémoire collective. Scan de secrets OBLIGATOIRE,
    diff avant envoi, privé par défaut."""
    from ghost.network import NetworkError, api_post, load_token
    from ghost.redact import redact
    from ghost.validate import skill_info

    token = load_token()
    if not token:
        console.print("connecte-toi d'abord : [bold]ghost login[/bold]")
        raise typer.Exit(1)
    conn = connect(db)
    try:
        skill_id = _resolve_skill_id(conn, skill_ref)
        skill = skill_info(conn, skill_id)
        if not skill.source.exists():
            console.print(f"[red]SKILL.md introuvable pour {skill.slug}[/red]")
            raise typer.Exit(1)
        raw = skill.source.read_text(encoding="utf-8")
        # Signature de TÂCHE (task_signature) et non la signature de détecteur du
        # candidat : c'est ce que `ghost retrieve` interroge (même espace que les
        # seeds). Sinon le skill publié serait introuvable.
        from ghost.signature import dominant_task_signature

        signature = dominant_task_signature(conn, skill.candidate_id)
    finally:
        conn.close()

    # Scan de secrets fail-closed AVANT tout envoi.
    body, counts = redact(raw)
    visibility = "public" if public else "private"
    console.print(
        f"[bold]Publish[/bold] {skill.slug} · visibility: [bold]{visibility}[/bold]"
    )
    if signature:
        console.print(f"[grey58]signature de tâche : {signature}[/grey58]")
    else:
        console.print(
            "[yellow]⚠ signature de tâche vide — ce skill ne sera pas trouvable "
            "par `ghost retrieve` (candidat sans session).[/yellow]"
        )
    if counts:
        console.print(f"[yellow]secret scan — masked:[/yellow] {counts}")
    else:
        console.print("[green]secret scan — nothing to mask[/green]")
    console.print("\n[dim]— exactly what will be sent (redacted) —[/dim]")
    lines = body.splitlines()
    for line in lines[:40]:
        console.print(f"  {line}", highlight=False)
    if len(lines) > 40:
        console.print("  [dim]… (preview truncated)[/dim]")
    if not yes and not typer.confirm(
        f"\nPublish {skill.slug} as {visibility}?", default=False
    ):
        raise typer.Exit(0)
    try:
        status, data = api_post(
            "/registry/publish", token,
            {
                "slug": skill.slug, "body": body, "summary": "",
                "signature": signature, "visibility": visibility,
            },
        )
    except NetworkError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if status != 200:
        console.print(f"[red]publish échoué ({status})[/red] : {data.get('detail')}")
        raise typer.Exit(1)
    console.print(f"\n[green]✓ published[/green] {skill.slug} ({visibility})")
    if visibility == "public":
        console.print(
            "[dim]Its lift will be measured and show up in `ghost earnings`.[/dim]"
        )


def main() -> None:
    app()
