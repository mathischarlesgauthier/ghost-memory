"""`ghost doctor` — diagnostic pour quelqu'un qui n'a pas écrit le code.

Chaque ✗ dit QUOI FAIRE, pas seulement ce qui manque. Aucune dépense, aucun
appel réseau : lecture seule du système de fichiers et de la base.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ghost.db import DEFAULT_DB
from ghost.ingest import DEFAULT_ROOT
from ghost.replay import API_KEY_FILE

GLOBAL_SKILLS_DIR = Path.home() / ".claude" / "skills"


@dataclass(slots=True)
class Check:
    ok: bool
    label: str
    detail: str
    fix: str = ""  # QUOI FAIRE si ✗


def _claude_cli() -> Check:
    path = shutil.which("claude")
    if path is None:
        return Check(
            False, "CLI claude", "introuvable dans le PATH",
            fix="installe Claude Code (https://claude.com/claude-code) ; "
            "ghost ingest/scan/distill fonctionnent sans, mais pas ghost validate.",
        )
    version = "?"
    with suppress(OSError, subprocess.SubprocessError):
        version = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    return Check(True, "CLI claude", f"{path} ({version})")


def _history(root: Path) -> Check:
    if not root.exists():
        return Check(
            False, "historique Claude Code", f"{root} n'existe pas",
            fix="utilise Claude Code au moins une fois ; l'historique s'écrit "
            f"dans {root}. Sinon passe --root vers ton dossier de projets.",
        )
    files = list(root.glob("*/*.jsonl"))
    if not files:
        return Check(
            False, "historique Claude Code", f"aucune session dans {root}",
            fix="lance quelques sessions Claude Code, puis relance ghost ingest.",
        )
    return Check(True, "historique Claude Code", f"{len(files)} session(s) top-level dans {root}")


def _database(db: Path) -> Check:
    if not db.exists():
        return Check(
            False, "base ~/.ghost/ghost.db", "pas encore créée",
            fix="lance `ghost ingest` pour la construire depuis ton historique.",
        )
    try:
        conn = sqlite3.connect(db)
        n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        dates = conn.execute(
            "SELECT MIN(started_at), MAX(ended_at) FROM sessions"
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return Check(
            False, "base ~/.ghost/ghost.db", f"illisible : {exc}",
            fix="supprime ~/.ghost/ghost.db et relance `ghost ingest --rebuild`.",
        )
    if n_sessions == 0:
        return Check(
            False, "base ~/.ghost/ghost.db", "base vide",
            fix="lance `ghost ingest` — l'historique n'a pas encore été importé.",
        )
    span = f"{(dates[0] or '?')[:10]} → {(dates[1] or '?')[:10]}"
    warn = "  (peu de sessions : signaux fragiles)" if n_sessions < 5 else ""
    return Check(
        n_sessions >= 3,
        "base ~/.ghost/ghost.db",
        f"{n_sessions} sessions · {n_events} events · {span}{warn}",
        fix="accumule plus de sessions Claude Code : sous ~3 sessions, les "
        "détecteurs n'ont pas de quoi trouver de vraies cicatrices."
        if n_sessions < 3 else "",
    )


def _api_key() -> Check:
    if not API_KEY_FILE.exists():
        return Check(
            False, "clé API Anthropic", f"absente de {API_KEY_FILE}",
            fix="nécessaire pour ghost distill/run/validate uniquement. "
            f"écris ta clé dans {API_KEY_FILE} (chmod 600) ou exporte "
            "ANTHROPIC_API_KEY. ghost ingest/scan/watch marchent sans.",
        )
    try:
        content = API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return Check(False, "clé API Anthropic", f"illisible : {exc}",
                     fix=f"vérifie les droits de {API_KEY_FILE} (chmod 600).")
    mode = oct(API_KEY_FILE.stat().st_mode & 0o777)
    if not content.startswith("sk-ant-"):
        return Check(
            False, "clé API Anthropic", "format inattendu (pas de préfixe sk-ant-)",
            fix="vérifie que le fichier contient une clé Anthropic valide.",
        )
    warn = "" if mode == "0o600" else f"  ⚠ droits {mode}, attendu 0o600"
    return Check(True, "clé API Anthropic", f"présente ({len(content)} car.){warn}",
                 fix=f"chmod 600 {API_KEY_FILE}" if mode != "0o600" else "")


def _writable(path: Path, what: str) -> Check:
    """Teste l'écriture RÉELLE (créer+supprimer un fichier), pas os.access —
    qui donne des faux positifs (W_OK sans X_OK, montages ro, ACLs)."""
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    if not target.is_dir():
        return Check(
            False, f"écriture {what}", f"{target} n'est pas un répertoire",
            fix=f"libère {target} : ghost a besoin d'y créer {path}.",
        )
    probe = target / ".ghost-write-test"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            False, f"écriture {what}", f"écriture impossible dans {target} ({exc.strerror})",
            fix=f"vérifie les permissions de {target} — ghost doit pouvoir y écrire.",
        )
    return Check(True, f"écriture {what}", f"OK ({path})")


def run_doctor(
    root: Path = DEFAULT_ROOT, db: Path = DEFAULT_DB
) -> list[Check]:
    return [
        _claude_cli(),
        _history(root),
        _database(db),
        _api_key(),
        _writable(db.parent, "~/.ghost/"),
        _writable(GLOBAL_SKILLS_DIR, "~/.claude/skills/"),
    ]
