"""Ingestion streaming des JSONL Claude Code vers SQLite.

Deux familles de fichiers sous ~/.claude/projects/ :
- sessions top-level :  <project>/<session-uuid>.jsonl
- transcripts subagents : <project>/<session-uuid>/subagents/agent-<id>.jsonl
  (+ sidecar agent-<id>.meta.json : agentType, description, toolUseId)

Idempotence : ingest_log(path → mtime, sha). Les fichiers de session sont
append-only et GROSSISSENT ; un fichier modifié est ré-ingéré intégralement
(DELETE de ses events + réinsertion) dans la même transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ghost.parse import line_meta, parse_line

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

_BATCH_SIZE = 500

_INSERT_EVENT = """
INSERT INTO events (session_id, agent_id, seq, ts, role, block_type, tool_name,
                    tool_use_id, is_error, is_human, text, payload_json,
                    payload_truncated, src_file, src_line)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_EventRow = tuple[
    str,
    str | None,
    int,
    str | None,
    str,
    str,
    str | None,
    str | None,
    int,
    int,
    str | None,
    str | None,
    int,
    str,
    int,
]


@dataclass(slots=True)
class SourceFile:
    """Un fichier JSONL à ingérer, avec son contexte déduit de son chemin."""

    path: Path
    project: str
    session_id: str
    agent_id: str | None = None
    meta_path: Path | None = None


@dataclass(slots=True)
class FileResult:
    status: str  # "ingested" | "unchanged" | "failed"
    n_events: int = 0
    n_skipped_lines: int = 0
    error: str | None = None


@dataclass(slots=True)
class Summary:
    n_files: int = 0
    n_ingested: int = 0
    n_unchanged: int = 0
    n_failed: int = 0
    n_events: int = 0
    n_skipped_lines: int = 0
    failures: list[tuple[str, str]] | None = None


def scan_files(root: Path = DEFAULT_ROOT) -> list[SourceFile]:
    """Liste les fichiers à ingérer, sessions top-level d'abord.

    L'ordre garantit que la ligne session existe avant ses subagents
    (un stub est créé sinon).
    """
    out: list[SourceFile] = []
    for path in sorted(root.glob("*/*.jsonl")):
        out.append(SourceFile(path=path, project=path.parent.name, session_id=path.stem))
    # Transcripts d'agents, à profondeur variable sous <session>/subagents/ :
    # subagents/agent-*.jsonl (Agent tool) et
    # subagents/workflows/wf_*/agent-*.jsonl (agents de Workflow).
    # Les journal.jsonl des workflows (orchestration, pas des conversations)
    # sont exclus par le motif agent-*.
    for path in sorted(root.glob("*/*/subagents/**/agent-*.jsonl")):
        rel = path.relative_to(root)
        meta_path = path.with_name(path.stem + ".meta.json")
        out.append(
            SourceFile(
                path=path,
                project=rel.parts[0],
                session_id=rel.parts[1],
                agent_id=path.stem.removeprefix("agent-"),
                meta_path=meta_path if meta_path.exists() else None,
            )
        )
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_json_lines(path: Path) -> Iterator[tuple[int, dict[str, object] | None]]:
    """(numéro de ligne 1-based, objet ou None si JSON invalide)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                yield lineno, None
                continue
            yield lineno, obj if isinstance(obj, dict) else None


def _upsert_agent(conn: sqlite3.Connection, source: SourceFile) -> None:
    agent_type: str | None = None
    description: str | None = None
    tool_use_id: str | None = None
    if source.meta_path is not None:
        try:
            meta = json.loads(source.meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = None
        if isinstance(meta, dict):
            agent_type = meta.get("agentType") if isinstance(meta.get("agentType"), str) else None
            description = (
                meta.get("description") if isinstance(meta.get("description"), str) else None
            )
            tool_use_id = meta.get("toolUseId") if isinstance(meta.get("toolUseId"), str) else None
    conn.execute(
        """
        INSERT INTO agents (agent_id, session_id, agent_type, description, tool_use_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            session_id=excluded.session_id,
            agent_type=COALESCE(excluded.agent_type, agent_type),
            description=COALESCE(excluded.description, description),
            tool_use_id=COALESCE(excluded.tool_use_id, tool_use_id)
        """,
        (source.agent_id, source.session_id, agent_type, description, tool_use_id),
    )


def _refresh_session_aggregates(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        """
        UPDATE sessions SET
            n_events   = (SELECT COUNT(*) FROM events WHERE session_id = ?),
            started_at = (SELECT MIN(ts) FROM events WHERE session_id = ? AND ts IS NOT NULL),
            ended_at   = (SELECT MAX(ts) FROM events WHERE session_id = ? AND ts IS NOT NULL)
        WHERE id = ?
        """,
        (session_id, session_id, session_id, session_id),
    )


def ingest_file(conn: sqlite3.Connection, source: SourceFile) -> FileResult:
    """Ingère un fichier dans une transaction unique. Jamais de crash sur
    une ligne corrompue : warning compté, ligne sautée."""
    src_file = str(source.path)
    stat = source.path.stat()
    row = conn.execute(
        "SELECT mtime, sha FROM ingest_log WHERE path = ?", (src_file,)
    ).fetchone()
    if row is not None and abs(float(row[0]) - stat.st_mtime) < 1e-6:
        return FileResult(status="unchanged")
    sha = _sha256(source.path)
    if row is not None and str(row[1]) == sha:
        conn.execute("UPDATE ingest_log SET mtime = ? WHERE path = ?", (stat.st_mtime, src_file))
        conn.commit()
        return FileResult(status="unchanged")

    sidechain = source.agent_id is not None
    n_events = 0
    n_skipped = 0
    seq = 0
    title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    batch: list[_EventRow] = []
    path_rows: list[tuple[int, str, str]] = []  # (offset dans le batch courant, path, op)

    def flush() -> None:
        nonlocal batch, path_rows
        if not batch:
            return
        cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events")
        base_row = cursor.fetchone()
        base_id = int(base_row[0]) if base_row is not None else 0
        conn.executemany(_INSERT_EVENT, batch)
        # Les ids sont séquentiels dans une transaction mono-connexion :
        # base_id + offset + 1 = id de l'event inséré à cet offset.
        if path_rows:
            conn.executemany(
                "INSERT INTO files_touched (event_id, path, op) VALUES (?, ?, ?)",
                [(base_id + offset + 1, path, op) for offset, path, op in path_rows],
            )
        batch = []
        path_rows = []

    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM files_touched WHERE event_id IN "
            "(SELECT id FROM events WHERE src_file = ?)",
            (src_file,),
        )
        conn.execute("DELETE FROM events WHERE src_file = ?", (src_file,))

        for lineno, obj in _iter_json_lines(source.path):
            if obj is None:
                n_skipped += 1
                continue
            if obj.get("type") == "ai-title" and isinstance(obj.get("aiTitle"), str):
                title = str(obj["aiTitle"])
                continue
            blocks = parse_line(obj, sidechain=sidechain)
            if not blocks:
                continue
            meta = line_meta(obj)
            cwd = meta.cwd or cwd
            git_branch = meta.git_branch or git_branch
            version = meta.version or version
            for block in blocks:
                seq += 1
                for path, op in block.paths:
                    path_rows.append((len(batch), path, op))
                batch.append(
                    (
                        source.session_id,
                        source.agent_id,
                        seq,
                        meta.ts,
                        block.role,
                        block.block_type,
                        block.tool_name,
                        block.tool_use_id,
                        block.is_error,
                        block.is_human,
                        block.text,
                        block.payload_json,
                        block.payload_truncated,
                        src_file,
                        lineno,
                    )
                )
                n_events += 1
                if len(batch) >= _BATCH_SIZE:
                    flush()
        flush()

        if sidechain:
            conn.execute(
                "INSERT INTO sessions (id, project) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                (source.session_id, source.project),
            )
            _upsert_agent(conn, source)
        else:
            conn.execute(
                """
                INSERT INTO sessions (id, project, cwd, git_branch, title, claude_version)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project        = excluded.project,
                    cwd            = COALESCE(excluded.cwd, cwd),
                    git_branch     = COALESCE(excluded.git_branch, git_branch),
                    title          = COALESCE(excluded.title, title),
                    claude_version = COALESCE(excluded.claude_version, claude_version)
                """,
                (source.session_id, source.project, cwd, git_branch, title, version),
            )
        _refresh_session_aggregates(conn, source.session_id)
        conn.execute(
            """
            INSERT INTO ingest_log (path, mtime, sha, ingested_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime = excluded.mtime, sha = excluded.sha, ingested_at = excluded.ingested_at
            """,
            (src_file, stat.st_mtime, sha, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    except Exception as exc:  # un fichier cassé ne doit pas tuer le run
        conn.rollback()
        return FileResult(status="failed", error=f"{type(exc).__name__}: {exc}")
    return FileResult(status="ingested", n_events=n_events, n_skipped_lines=n_skipped)


def ingest_all(
    conn: sqlite3.Connection,
    root: Path = DEFAULT_ROOT,
) -> Iterator[tuple[SourceFile, FileResult]]:
    """Ingère tout le corpus, en streaming fichier par fichier."""
    for source in scan_files(root):
        yield source, ingest_file(conn, source)
