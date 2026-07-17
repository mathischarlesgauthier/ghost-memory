"""Télémétrie — opt-in explicite, un seul POST, ALLOWLIST STRICTE.

Envoyé : nb de sessions/events, candidats par kind, langages (extensions →
langage), verbes de commande d'une allowlist fermée, classes d'erreur d'une
allowlist fermée, nb de skills. JAMAIS : prompts, code, chemins, noms de
fichiers, contenu de skills — garanti PAR CONSTRUCTION (rien de textuel libre
ne sort ; tout est classifié dans un ensemble fixe). La revue adversariale a
prouvé qu'une normalisation par blocklist laissait fuir clés en casse mixte,
mots de passe et noms de fichiers ; d'où l'allowlist.

Le payload est affiché avant tout envoi (`ghost telemetry preview`). Endpoint
https requis (http toléré uniquement en local). Pas de SaaS : urllib stdlib,
un POST JSON vers un endpoint que l'utilisateur configure. Off par défaut.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ghost.detect import bash_token, tool_input

CONFIG_FILE = Path.home() / ".ghost" / "telemetry.json"
_EXT_LANG = {
    "py": "python", "tsx": "typescript", "ts": "typescript", "jsx": "javascript",
    "js": "javascript", "mjs": "javascript", "go": "go", "rs": "rust",
    "java": "java", "kt": "kotlin", "rb": "ruby", "php": "php", "cs": "csharp",
    "swift": "swift", "c": "c", "cpp": "cpp", "sql": "sql", "sh": "shell",
    "css": "css", "html": "html",
}

# Verbes de TÊTE autorisés : seul le premier mot de bash_token
# (`Bash:git-commit` → `git`, `Bash:uv-run-pytest` → `uv`) est émis, et
# uniquement s'il figure ici. Tout le reste (y compris `Bash:python-<fichier>`
# dont la tête serait un nom de script) devient "other" — aucun nom de fichier
# ne peut jamais franchir cette barrière.
_ALLOWED_HEADS = frozenset(
    {
        "git", "uv", "npm", "pnpm", "npx", "pytest", "docker", "cargo", "go",
        "make", "python", "python3", "node", "tsc", "ruff", "mypy", "ls",
        "grep", "find", "cat", "sed", "awk", "cd", "echo", "mkdir", "rm",
        "cp", "mv", "curl", "wget", "rustc", "gradle", "mvn",
    }
)

# Classes d'erreur : une erreur est réduite à UNE étiquette de cet ensemble
# fixe, jamais à son texte. Ordre = priorité.
_ERROR_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("file_stale", re.compile(r"has been modified since read", re.I)),
    ("file_not_read", re.compile(r"has not been read yet", re.I)),
    ("edit_no_match", re.compile(r"string to replace not found", re.I)),
    ("file_not_found", re.compile(r"file (does not|doesn't) exist|no such file", re.I)),
    ("module_not_found", re.compile(r"modulenotfound|cannot find (module|package)", re.I)),
    ("import_error", re.compile(r"importerror|cannot import", re.I)),
    ("syntax_error", re.compile(r"syntaxerror|parse error|unexpected token", re.I)),
    ("type_error", re.compile(r"typeerror|type '.*' is not", re.I)),
    ("name_error", re.compile(r"nameerror|is not defined|undefined variable", re.I)),
    ("assertion", re.compile(r"assertionerror|assert", re.I)),
    ("test_failure", re.compile(r"failed|test.*fail|\bfail(ed|ure)\b", re.I)),
    ("timeout", re.compile(r"timeout|timed out", re.I)),
    ("permission", re.compile(r"permission denied|not permitted|denied by", re.I)),
    ("connection", re.compile(r"connection|econnrefused|network|unreachable", re.I)),
    ("schema_error", re.compile(r"does not match required schema|must have required", re.I)),
    ("tool_use_error", re.compile(r"tool_use_error", re.I)),
    ("interrupted", re.compile(r"request interrupted", re.I)),
    ("exit_nonzero", re.compile(r"^exit code [1-9]", re.I)),
)


def classify_error(text: str) -> str:
    for label, pattern in _ERROR_CLASSES:
        if pattern.search(text):
            return label
    return "other"


def safe_command_family(command: str) -> str:
    """Verbe de TÊTE de la commande, réduit à l'allowlist — jamais un nom de
    fichier (ni le reste de la commande)."""
    token = bash_token(command)
    if ":" not in token:
        return "bash-bare"
    head = token.split(":", 1)[1].split("-", 1)[0]
    return f"bash-{head}" if head in _ALLOWED_HEADS else "bash-other"


@dataclass(slots=True)
class TelemetryConfig:
    enabled: bool = False
    endpoint: str | None = None
    install_id: str = ""

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> TelemetryConfig:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            enabled=bool(data.get("enabled")),
            endpoint=data.get("endpoint") if isinstance(data.get("endpoint"), str) else None,
            install_id=str(data.get("install_id", "")),
        )

    def save(self, path: Path = CONFIG_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self.install_id:
            self.install_id = uuid.uuid4().hex
        path.write_text(
            json.dumps(
                {"enabled": self.enabled, "endpoint": self.endpoint,
                 "install_id": self.install_id},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)


@dataclass(slots=True)
class Payload:
    install_id: str
    ghost_version: str
    sent_at: str
    n_sessions: int
    n_events: int
    candidates_by_kind: dict[str, int] = field(default_factory=dict)
    languages: dict[str, int] = field(default_factory=dict)
    command_families: dict[str, int] = field(default_factory=dict)
    error_classes: dict[str, int] = field(default_factory=dict)
    n_skills: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "install_id": self.install_id,
            "ghost_version": self.ghost_version,
            "sent_at": self.sent_at,
            "n_sessions": self.n_sessions,
            "n_events": self.n_events,
            "candidates_by_kind": self.candidates_by_kind,
            "languages": self.languages,
            "command_families": self.command_families,
            "error_classes": self.error_classes,
            "n_skills": self.n_skills,
        }


def build_payload(
    conn: sqlite3.Connection, config: TelemetryConfig, ghost_version: str
) -> Payload:
    """Agrège des COMPTES uniquement — aucune donnée textuelle brute ne sort."""
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    by_kind = {
        str(kind): int(n)
        for kind, n in conn.execute(
            "SELECT kind, COUNT(*) FROM candidates WHERE status != 'rejected' GROUP BY kind"
        )
    }
    n_skills = conn.execute(
        "SELECT COUNT(*) FROM skills WHERE verdict = 'SKILL'"
    ).fetchone()[0]

    # Langages : extensions touchées → langage (jamais le chemin).
    langs: Counter[str] = Counter()
    for (path,) in conn.execute("SELECT path FROM files_touched"):
        suffix = str(path).rsplit(".", 1)[-1].lower() if "." in str(path) else ""
        if suffix in _EXT_LANG:
            langs[_EXT_LANG[suffix]] += 1

    # Familles de commandes : verbe d'une allowlist fermée (jamais un fichier).
    families: Counter[str] = Counter()
    for (payload,) in conn.execute(
        "SELECT payload_json FROM events WHERE block_type = 'tool_use' "
        "AND tool_name = 'Bash'"
    ):
        command = tool_input(str(payload) if payload else None).get("command")
        if isinstance(command, str):
            families[safe_command_family(command)] += 1

    # Erreurs : classées dans un ensemble fixe, jamais leur texte.
    classes: Counter[str] = Counter()
    for (text,) in conn.execute(
        "SELECT text FROM events WHERE block_type = 'tool_result' "
        "AND is_error = 1 AND text IS NOT NULL LIMIT 20000"
    ):
        classes[classify_error(str(text))] += 1

    return Payload(
        install_id=config.install_id or "anonyme",
        ghost_version=ghost_version,
        sent_at=datetime.now(UTC).isoformat(),
        n_sessions=int(n_sessions),
        n_events=int(n_events),
        candidates_by_kind=by_kind,
        languages=dict(langs.most_common(20)),
        command_families=dict(families.most_common(40)),
        error_classes=dict(classes.most_common()),
        n_skills=int(n_skills),
    )


def validate_endpoint(endpoint: str) -> tuple[bool, str]:
    """https requis ; http toléré uniquement en local (self-hosting)."""
    if endpoint.startswith("https://"):
        return True, "ok"
    if endpoint.startswith("http://"):
        host = endpoint[len("http://") :].split("/", 1)[0].split(":", 1)[0]
        if host in ("localhost", "127.0.0.1", "::1"):
            return True, "ok (http local)"
        return False, "http:// non chiffré interdit hors localhost — utilise https://"
    return False, "endpoint invalide (https:// requis)"


def send(payload: Payload, endpoint: str, *, timeout: float = 10.0) -> tuple[bool, str]:
    ok_endpoint, reason = validate_endpoint(endpoint)
    if not ok_endpoint:
        return False, reason
    body = json.dumps(payload.to_dict()).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)
