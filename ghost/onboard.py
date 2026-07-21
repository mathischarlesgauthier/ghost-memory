"""Helpers d'onboarding (`ghost init`) — logique testable, sans I/O interactif.

Le premier kilomètre tuait 4 frictions avant la première valeur (PATH, clé API,
localisation, historique). Ces helpers isolent la logique ; la commande `init`
n'orchestre que les prompts.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

API_KEY_FILE = Path.home() / ".ghost" / "api_key"


def ghost_on_path() -> bool:
    return shutil.which("ghost") is not None


def detect_shell_rc() -> Path:
    """Le fichier rc à éditer pour ajouter ~/.local/bin au PATH."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    if "fish" in shell:
        return home / ".config" / "fish" / "config.fish"
    return home / ".profile"


def write_api_key(key: str, path: Path = API_KEY_FILE) -> Path:
    """Écrit la clé en 0600, dossier en 0700. Renvoie le chemin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(key.strip() + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class ApiKeyMissing(RuntimeError):
    """Aucune clé Anthropic locale : ni ~/.ghost/api_key, ni ANTHROPIC_API_KEY."""


def resolve_api_key() -> str:
    """Clé Anthropic locale : le fichier écrit par `ghost init` d'abord (source
    de vérité), sinon ANTHROPIC_API_KEY. Lève ApiKeyMissing avec la consigne —
    jamais laisser le SDK planter en « Could not resolve authentication method »."""
    if API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    raise ApiKeyMissing(
        f"clé Anthropic introuvable ({API_KEY_FILE} absent, ANTHROPIC_API_KEY "
        "non défini) — lance `ghost init`"
    )


@dataclass(slots=True)
class HistoryStatus:
    projects_exist: bool
    n_files: int


def history_status(root: Path) -> HistoryStatus:
    if not root.exists():
        return HistoryStatus(False, 0)
    return HistoryStatus(True, sum(1 for _ in root.glob("**/*.jsonl")))


def ping_api_key(key: str) -> tuple[bool, str]:
    """Valide la clé par un appel métadonnées (GET /v1/models) — aucun token
    consommé. Renvoie (ok, message)."""
    try:
        import anthropic
    except Exception as exc:  # SDK indisponible : ne bloque pas l'onboarding
        return False, f"SDK anthropic indisponible ({exc})"
    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError:
        return False, "clé refusée (401) — vérifie-la sur console.anthropic.com"
    except anthropic.APIError as exc:
        return False, f"erreur API ({type(exc).__name__})"
    except Exception as exc:  # réseau, etc. — informatif, non bloquant
        return False, f"validation impossible ({exc})"
    return True, "clé valide"
