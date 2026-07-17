"""Parsing d'une ligne JSONL Claude Code vers des events normalisés.

Format constaté sur corpus réel (versions 2.1.170 → 2.1.211), voir rapport
de reconnaissance : seules les lignes `user`, `assistant`, `system` portent
du contenu conversationnel ; les 13 types annexes (mode, ai-title,
file-history-snapshot, attachment, queue-operation, …) sont ignorés, sauf
`ai-title` qui alimente sessions.title.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

PAYLOAD_CAP = 65_536
"""Taille max (caractères) de payload_json et text ; au-delà, troncature
avec flag payload_truncated=1. La ligne brute intégrale reste accessible
via (src_file, src_line)."""

MESSAGE_TYPES = frozenset({"user", "assistant", "system"})

DENY_TAGS: tuple[str, ...] = (
    "<task-notification>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<system-reminder>",
)
"""Préfixes de messages `user` générés par le harness, pas par l'humain.
Deny-list par tag exact : un humain peut coller du texte commençant par `<`."""

_PATH_TOOLS: dict[str, str] = {"Edit": "edit", "Write": "write", "Read": "read"}


@dataclass(slots=True)
class Block:
    """Un event en devenir : un bloc de content (ou une ligne system)."""

    role: str
    block_type: str
    tool_name: str | None = None
    tool_use_id: str | None = None
    is_error: int = 0
    is_human: int = 0
    text: str | None = None
    payload_json: str | None = None
    payload_truncated: int = 0
    paths: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class LineMeta:
    """Métadonnées de session portées par les lignes message."""

    ts: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None


def is_human_text(text: str) -> bool:
    """Vrai si le texte n'est pas une injection connue du harness."""
    stripped = text.lstrip()
    return not any(stripped.startswith(tag) for tag in DENY_TAGS)


def _truncate(value: str) -> tuple[str, int]:
    if len(value) > PAYLOAD_CAP:
        return value[:PAYLOAD_CAP], 1
    return value, 0


def _dump(obj: object) -> tuple[str, int]:
    return _truncate(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _flatten_result_content(content: object) -> str | None:
    """Aplati le content d'un tool_result (string, ou liste de blocs text)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
    return None


def _extract_paths(tool_name: str, tool_input: object) -> list[tuple[str, str]]:
    op = _PATH_TOOLS.get(tool_name)
    if op is None or not isinstance(tool_input, Mapping):
        return []
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path:
        return [(file_path, op)]
    return []


def _opt_str(obj: Mapping[str, object], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def line_meta(obj: Mapping[str, object]) -> LineMeta:
    return LineMeta(
        ts=_opt_str(obj, "timestamp"),
        cwd=_opt_str(obj, "cwd"),
        git_branch=_opt_str(obj, "gitBranch"),
        version=_opt_str(obj, "version"),
    )


def _block_from_content_block(raw: Mapping[str, object], role: str, human_ok: bool) -> Block:
    block_type = _opt_str(raw, "type") or "unknown"
    payload, truncated = _dump(raw)
    block = Block(
        role=role,
        block_type=block_type,
        payload_json=payload,
        payload_truncated=truncated,
    )
    if block_type == "thinking":
        text = _opt_str(raw, "thinking")
        block.text, text_truncated = _truncate(text) if text is not None else (None, 0)
        block.payload_truncated |= text_truncated
    elif block_type == "text":
        text = _opt_str(raw, "text")
        if text is not None:
            block.text, text_truncated = _truncate(text)
            block.payload_truncated |= text_truncated
            block.is_human = 1 if human_ok and is_human_text(text) else 0
    elif block_type == "image":
        block.is_human = 1 if human_ok else 0
    elif block_type == "tool_use":
        block.tool_name = _opt_str(raw, "name")
        block.tool_use_id = _opt_str(raw, "id")
        if block.tool_name is not None:
            block.paths = _extract_paths(block.tool_name, raw.get("input"))
    elif block_type == "tool_result":
        block.tool_use_id = _opt_str(raw, "tool_use_id")
        # Règle vérifiée sur 8 186 tool_results réels : is_error absent ou
        # False = succès ; aucun cas "absent mais texte d'erreur" observé.
        block.is_error = 1 if raw.get("is_error") is True else 0
        text = _flatten_result_content(raw.get("content"))
        if text is not None:
            block.text, text_truncated = _truncate(text)
            block.payload_truncated |= text_truncated
    return block


def parse_line(obj: Mapping[str, object], *, sidechain: bool) -> list[Block]:
    """Transforme une ligne message (user/assistant/system) en blocs-events.

    Retourne une liste vide pour les types annexes. `sidechain` force
    is_human=0 : dans un transcript de subagent, le "user" est l'agent
    parent, jamais l'humain.
    """
    line_type = _opt_str(obj, "type")
    if line_type not in MESSAGE_TYPES:
        return []

    if line_type == "system":
        payload, truncated = _dump(obj)
        return [
            Block(
                role="system",
                block_type=_opt_str(obj, "subtype") or "system",
                text=_opt_str(obj, "content"),
                payload_json=payload,
                payload_truncated=truncated,
            )
        ]

    message = obj.get("message")
    if not isinstance(message, Mapping):
        return []
    role = _opt_str(message, "role") or line_type
    human_ok = (
        role == "user"
        and not sidechain
        and not bool(obj.get("isMeta"))
        and not bool(obj.get("isCompactSummary"))
    )

    content = message.get("content")
    if isinstance(content, str):
        payload, truncated = _dump({"type": "text", "text": content})
        text, text_truncated = _truncate(content)
        return [
            Block(
                role=role,
                block_type="text",
                text=text,
                payload_json=payload,
                payload_truncated=truncated | text_truncated,
                is_human=1 if human_ok and is_human_text(content) else 0,
            )
        ]
    if isinstance(content, list):
        return [
            _block_from_content_block(raw, role, human_ok)
            for raw in content
            if isinstance(raw, Mapping)
        ]
    return []
