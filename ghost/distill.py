"""Distillation : candidat + trace brute → SKILL.md vendable.

Un appel LLM (claude-sonnet-5, choix validé), sortie contrainte par
json_schema — les paramètres de sampling n'existant plus sur Sonnet 5, la
« température basse » du cahier des charges se traduit par thinking désactivé
+ format de sortie strict. Second appel d'auto-critique sur le SKILL.md.

Le SKIP est un succès : un distillateur qui ne skippe jamais est un
générateur de bruit.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from ghost.trace import Trace, build_trace

MODEL = "claude-sonnet-5"
PROMPT_VERSION = "1"
# Prix sticker $3/$15 par MTok (intro $2/$10 jusqu'au 2026-08-31) ; on
# provisionne au sticker. Plafond validé : ~0,15 $ par distillation.
PRICE_IN = 3.0 / 1_000_000
PRICE_OUT = 15.0 / 1_000_000
MAX_TOKENS_DISTILL = 5_000
MAX_TOKENS_CRITIQUE = 800
COST_CAP_USD = 0.20  # garde-fou dur pré-appel (pire cas trace 30k + 5k out)

DEFAULT_SKILLS_DIR = Path.home() / ".ghost" / "skills"

SYSTEM_PROMPT = (
    "Tu distilles l'historique brut d'un agent de code (Claude Code) en un "
    "SKILL.md réutilisable.\n\n"
    "Règles absolues :\n"
    "- Tu écris pour un agent, pas pour un humain. Impératif, dense, pas de prose.\n"
    "- Chaque piège DOIT citer l'échec précis de la trace dont il vient (numéro "
    "d'event [N], message d'erreur ou correction humaine, verbatim court).\n"
    "- INTERDICTION d'écrire un conseil qui n'est pas prouvé par la trace. Pas de "
    "bonnes pratiques génériques : si un bon dev le sait déjà sans avoir vécu la "
    "trace, ça ne va pas dans le skill.\n"
    "- Si la trace ne prouve rien de non-évident : decision=SKIP avec la raison. "
    "Le SKIP est un succès, pas un échec.\n"
    "- name : slug court en kebab-case. description : UNE ligne orientée "
    "déclenchement (« Quand tu... »).\n"
    "- La langue de sortie est le français, sauf les identifiants techniques."
)

SKILL_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision", "skip_reason", "name", "description", "tags", "stack",
        "quand_utiliser", "procedure", "pieges", "anti_patterns",
    ],
    "properties": {
        "decision": {"type": "string", "enum": ["SKILL", "SKIP"]},
        "skip_reason": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "stack": {"type": "array", "items": {"type": "string"}},
        "quand_utiliser": {"type": "string"},
        "procedure": {"type": "array", "items": {"type": "string"}},
        "pieges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["piege", "preuve"],
                "properties": {
                    "piege": {"type": "string"},
                    "preuve": {"type": "string"},
                },
            },
        },
        "anti_patterns": {"type": "array", "items": {"type": "string"}},
    },
}

CRITIQUE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "ligne"],
    "properties": {
        "verdict": {"type": "string", "enum": ["OUI", "NON"]},
        "ligne": {"type": "string"},
    },
}

CRITIQUE_PROMPT = (
    "Ce skill contient-il UNE information qu'un bon dev ne devinerait pas sans "
    "avoir vécu la trace ? Réponds verdict=OUI avec la ligne concernée (citée), "
    "ou verdict=NON avec ligne vide."
)

# (system, user, schema, max_tokens) -> (données parsées, tokens_in, tokens_out)
LlmCaller = Callable[[str, str, dict[str, object], int], tuple[dict[str, object], int, int]]


class DistillError(RuntimeError):
    pass


@dataclass(slots=True)
class DistillResult:
    candidate_id: int
    verdict: str  # 'SKILL' | 'SKIP'
    skill_path: Path | None
    slug: str | None
    skill_md: str | None
    skip_reason: str | None
    low_value: bool
    critique_line: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    trace: Trace
    structural_problems: list[str] = field(default_factory=list)


def default_caller(client: anthropic.Anthropic) -> LlmCaller:
    def call(
        system: str, user: str, schema: dict[str, object], max_tokens: int
    ) -> tuple[dict[str, object], int, int]:
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "disabled"},
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            raise DistillError("le modèle a refusé la requête (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise DistillError(
                f"sortie tronquée à {max_tokens} tokens (stop_reason=max_tokens) — "
                "JSON incomplet, appel perdu"
            )
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise DistillError(f"réponse sans bloc text (stop_reason={response.stop_reason})")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DistillError(f"sortie structurée non parsable: {exc}") from exc
        if not isinstance(data, dict):
            raise DistillError("sortie structurée inattendue (pas un objet)")
        return data, response.usage.input_tokens, response.usage.output_tokens

    return call


def validate_structure(data: dict[str, object]) -> list[str]:
    """Gate mécanique à coût zéro, avant l'auto-critique LLM. Un SKILL sans
    section Pièges prouvée ne vaut rien — rejeté ici."""
    problems: list[str] = []
    if data.get("decision") == "SKIP":
        if not str(data.get("skip_reason", "")).strip():
            problems.append("SKIP sans skip_reason")
        return problems
    if not str(data.get("name", "")).strip():
        problems.append("name vide")
    if not str(data.get("description", "")).strip():
        problems.append("description vide")
    pieges = data.get("pieges")
    if not isinstance(pieges, list) or not pieges:
        problems.append("section Pièges vide — skill rejeté")
        return problems
    for i, item in enumerate(pieges):
        if not isinstance(item, dict):
            problems.append(f"piège {i}: format invalide")
            continue
        if len(str(item.get("piege", "")).strip()) < 10:
            problems.append(f"piège {i}: trop court ou vide")
        if len(str(item.get("preuve", "")).strip()) < 15:
            problems.append(f"piège {i}: preuve absente ou non citée")
    return problems


def _slugify(name: str, skills_dir: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "skill"
    candidate, n = slug, 2
    while (skills_dir / candidate).exists():
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def render_skill_md(data: dict[str, object]) -> str:
    def strlist(key: str) -> list[str]:
        value = data.get(key)
        return [str(x) for x in value] if isinstance(value, list) else []

    lines = [
        "---",
        f"name: {data.get('name', '')}",
        f"description: {data.get('description', '')}",
        f"tags: [{', '.join(strlist('tags'))}]",
        f"stack: [{', '.join(strlist('stack'))}]",
        "---",
        "",
        "## Quand utiliser",
        str(data.get("quand_utiliser", "")).strip(),
        "",
        "## Procédure",
    ]
    lines += [f"{i}. {step}" for i, step in enumerate(strlist("procedure"), start=1)]
    lines += ["", "## Pièges"]
    pieges = data.get("pieges")
    if isinstance(pieges, list):
        for item in pieges:
            if isinstance(item, dict):
                lines.append(f"- **{item.get('piege', '')}**")
                lines.append(f"  - Preuve : {item.get('preuve', '')}")
    lines += ["", "## Anti-patterns"]
    lines += [f"- {ap}" for ap in strlist("anti_patterns")]
    return "\n".join(lines) + "\n"


def distill(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    caller: LlmCaller,
    skills_dir: Path = DEFAULT_SKILLS_DIR,
) -> DistillResult:
    trace = build_trace(conn, candidate_id)
    if trace.n_occurrences == 0:
        raise DistillError(
            "trace sans aucune occurrence résolue (évidence périmée ? relancer ghost scan)"
        )
    # est_tokens = chars/4 sous-estime le tokenizer Sonnet 5 (~30 % plus dense
    # sur du français/code) : on provisionne à 1.35x.
    projected = trace.est_tokens * 1.35 * PRICE_IN + MAX_TOKENS_DISTILL * PRICE_OUT
    if projected > COST_CAP_USD:
        raise DistillError(
            f"coût projeté {projected:.2f}$ > plafond {COST_CAP_USD}$ — trace trop grosse"
        )

    user_prompt = (
        f"Voici la trace brute du candidat (kind={trace.kind}).\n\n{trace.text}\n\n"
        "Distille. Rappel : chaque piège cite son échec ; SKIP si rien de non-évident."
    )
    data, tokens_in, tokens_out = caller(
        SYSTEM_PROMPT, user_prompt, SKILL_SCHEMA, MAX_TOKENS_DISTILL
    )
    cost = tokens_in * PRICE_IN + tokens_out * PRICE_OUT

    problems = validate_structure(data)
    verdict = str(data.get("decision", "SKIP"))
    if verdict == "SKILL" and problems:
        # Gate structurel : un SKILL invalide est requalifié en SKIP mécanique.
        verdict = "SKIP"
        data["skip_reason"] = "rejet structurel: " + "; ".join(problems)

    result = DistillResult(
        candidate_id=candidate_id,
        verdict=verdict,
        skill_path=None,
        slug=None,
        skill_md=None,
        skip_reason=str(data.get("skip_reason") or "") or None,
        low_value=False,
        critique_line="",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        trace=trace,
        structural_problems=problems,
    )

    if verdict == "SKILL":
        skill_md = render_skill_md(data)
        # Auto-critique (même passe) : NON -> low_value, on ne supprime pas.
        # Un échec de la critique n'invalide pas le skill déjà payé.
        try:
            critique, c_in, c_out = caller(
                SYSTEM_PROMPT, f"{CRITIQUE_PROMPT}\n\nSKILL.md :\n{skill_md}",
                CRITIQUE_SCHEMA, MAX_TOKENS_CRITIQUE,
            )
        except DistillError as exc:
            result.critique_line = f"<critique échouée: {exc}>"
        else:
            result.tokens_in += c_in
            result.tokens_out += c_out
            result.cost_usd += c_in * PRICE_IN + c_out * PRICE_OUT
            result.low_value = critique.get("verdict") == "NON"
            result.critique_line = str(critique.get("ligne", ""))

        try:
            skills_dir.mkdir(parents=True, exist_ok=True)
            slug = _slugify(str(data.get("name", "skill")), skills_dir)
            skill_dir = skills_dir / slug
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        except OSError as exc:
            # On persiste quand même la comptabilité des tokens déjà payés.
            _persist(conn, result)
            raise DistillError(f"écriture du skill impossible: {exc}") from exc
        result.skill_path = skill_dir / "SKILL.md"
        result.slug = slug
        result.skill_md = skill_md

    _persist(conn, result)
    return result


def _persist(conn: sqlite3.Connection, result: DistillResult) -> None:
    conn.execute(
        """
        INSERT INTO skills (candidate_id, slug, path, model, prompt_version, verdict,
                            low_value, skip_reason, critique_line, tokens_in, tokens_out,
                            cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.candidate_id,
            result.slug,
            str(result.skill_path) if result.skill_path else None,
            MODEL,
            PROMPT_VERSION,
            result.verdict,
            int(result.low_value),
            result.skip_reason,
            result.critique_line,
            result.tokens_in,
            result.tokens_out,
            round(result.cost_usd, 6),
            datetime.now(UTC).isoformat(),
        ),
    )
    if result.verdict == "SKILL":
        conn.execute(
            "UPDATE candidates SET status = 'distilled' WHERE id = ?",
            (result.candidate_id,),
        )
    conn.commit()
