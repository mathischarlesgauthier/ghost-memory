"""Redaction avant tout envoi LLM — fail closed : dans le doute, on masque.

Regex + deny-list (~/.ghost/deny.txt, une chaîne littérale par ligne).
On logge des COMPTES de redactions, jamais les valeurs. La sur-redaction est
acceptée par contrat ; la sous-redaction est un bug. Une deny-list présente
mais illisible interrompt le pipeline (RedactionError) au lieu d'être
silencieusement ignorée.
"""

from __future__ import annotations

import re
from pathlib import Path

DENY_FILE = Path.home() / ".ghost" / "deny.txt"


class RedactionError(RuntimeError):
    """Deny-list présente mais illisible : on refuse d'envoyer quoi que ce soit."""


# Les patterns à groupe conservent le préfixe (\1) et masquent la valeur.
_KEEP_PREFIX = frozenset({"auth_header", "url_token", "slack_webhook", "url_creds", "env_secret"})

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r".*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
    ),
    (
        "api_key",
        re.compile(
            r"\b(?:sk-ant-[\w\-]{10,}|sk-[\w\-]{20,}|ghp_[A-Za-z0-9]{20,}"
            r"|gho_[A-Za-z0-9]{20,}|github_pat_[\w]{20,}|xox[bapors]-[\w\-]{10,}"
            r"|xapp-[\w\-]{10,}|xoxe-[\w\-]{10,}|AKIA[0-9A-Z]{16}|AIza[\w\-]{30,}"
            r"|whsec_[A-Za-z0-9]{16,}|[psr]k_(?:live|test)_[A-Za-z0-9]{16,}"
            r"|ntn_[A-Za-z0-9]{20,}|ya29\.[\w\-.]{20,})\b"
        ),
    ),
    ("jwt", re.compile(r"\beyJ[\w\-]{10,}\.[\w\-]{10,}\.[\w\-]{5,}\b")),
    # Authorization: Bearer/Token/Basic xxx — où que ce soit dans la ligne
    ("auth_header", re.compile(r"(?i)\b((?:bearer|token|basic)\s+)[\w.\-=+/]{15,}")),
    # ?token= / &access_token= / ?apikey= … dans les URLs
    (
        "url_token",
        re.compile(
            r"(?i)([?&](?:token|access_token|apikey|api_key|key|secret|signature|sig"
            r"|password|auth)=)[^&\s\"']+"
        ),
    ),
    ("slack_webhook", re.compile(r"(hooks\.slack\.com/services/)[\w/]+")),
    # postgres://user:PASS@host — on masque le mot de passe du userinfo
    ("url_creds", re.compile(r"([a-zA-Z][\w+.\-]*://[^/\s:@]+:)[^@\s/]+(?=@)")),
    # KEY=val, KEY: "val", "api_key": "val" — PAS ancré en début de ligne :
    # les traces préfixent chaque ligne par « [seq] outil X: ».
    (
        "env_secret",
        re.compile(
            r"(?i)((?:export\s+)?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD"
            r"|CREDENTIAL)[A-Z0-9_]*[\"']?\s*[=:]\s*[\"']?)[^\s\"']{4,}"
        ),
    ),
    ("email", re.compile(r"\b[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)

_HOME_RE = re.compile(re.escape(str(Path.home())))


def _deny_list(deny_file: Path) -> list[str]:
    if not deny_file.exists():
        return []
    try:
        lines = deny_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RedactionError(
            f"deny-list {deny_file} présente mais illisible — fail closed, envoi refusé"
        ) from exc
    return [
        stripped
        for ln in lines
        if (stripped := ln.strip()) and not stripped.startswith("#")
    ]


def redact(text: str, deny_file: Path = DENY_FILE) -> tuple[str, dict[str, int]]:
    """Masque secrets, identifiants et chemins personnels. Retourne le texte
    nettoyé et les comptes par catégorie (jamais les valeurs)."""
    counts: dict[str, int] = {}

    for literal in _deny_list(deny_file):
        n = text.count(literal)
        if n:
            counts["deny_list"] = counts.get("deny_list", 0) + n
            text = text.replace(literal, "<redacted:deny>")

    for name, pattern in _PATTERNS:
        keep = name in _KEEP_PREFIX
        replacement = rf"\1<redacted:{name}>" if keep else f"<redacted:{name}>"
        text, n = pattern.subn(replacement, text)
        if n:
            counts[name] = counts.get(name, 0) + n

    text, n = _HOME_RE.subn("~", text)
    if n:
        counts["home_path"] = n
    return text, counts
