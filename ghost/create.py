"""`ghost create <lien-github>` : importe un skill hébergé sur GitHub, le
normalise au format Ghost, et l'ajoute aux skills LOCAUX (comme un distillé maison).

Copie assumée de la logique de normalisation du backend (`app/normalize.py`) — les
deux dépôts sont indépendants, pas de paquet partagé. Adaptée ici :
  - au **format des skills distillés maison** (frontmatter `name/description/tags/
    stack` en listes, comme `distill.render_skill_md`) + lignes de provenance
    `source`/`license` ;
  - au **stockage local** (une ligne `candidates` + une ligne `skills` + le
    SKILL.md dans ~/.ghost/skills/<slug>/) pour que `ghost skills`, `ghost deploy`,
    `ghost publish` et `ghost retrieve` le traitent EXACTEMENT comme un distillé.

Le task_signature généré est stocké sur le candidat (colonne `task_signature`) :
`dominant_task_signature` le renvoie quand le candidat n'a pas de session (cas
import), donc `ghost publish`/`retrieve` restent inchangés.

L'appel LLM réutilise `distill.default_caller` (même modèle Sonnet 5, même contrat
de sortie contrainte). SKIP conservé : un contenu générique/du bruit est refusé
avec sa raison, comme la distillation normale.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from ghost.distill import (
    MODEL,
    PRICE_IN,
    PRICE_OUT,
    DistillError,
    LlmCaller,
    _slugify,
)

PROMPT_VERSION = "create-1"
_MAX_TOKENS = 900
_MAX_INPUT_CHARS = 24_000

_ALLOWED = {"github.com", "www.github.com", "raw.githubusercontent.com"}
_MAX_BYTES = 512 * 1024  # 512 KiB
_SEG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SLUG_SPLIT = re.compile(r"[^a-z0-9]+")


class CreateError(RuntimeError):
    """Import impossible — remonté proprement à l'utilisateur, sans trace."""


# ── Fetch GitHub blindé SSRF (port de app/github_fetch.py) ────────────────────
def _check(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise CreateError("https requis")
    if parts.hostname not in _ALLOWED:
        raise CreateError(f"hôte non autorisé : {parts.hostname or '?'} (github/raw uniquement)")


def resolve_github_raw(url: str) -> str:
    """Page GitHub `/blob/` → URL raw. Une URL raw passe telle quelle."""
    _check(url)
    parts = urlsplit(url)
    if parts.hostname in {"github.com", "www.github.com"}:
        segs = list(parts.path.split("/"))
        # /{owner}/{repo}/blob/{ref}/{chemin...}
        if len(segs) >= 6 and segs[3] == "blob":
            new_path = "/" + "/".join([segs[1], segs[2], *segs[4:]])
            return urlunsplit(("https", "raw.githubusercontent.com", new_path, "", ""))
        raise CreateError(
            "lien GitHub non résoluble : attendu une page de fichier /blob/… ou un lien raw"
        )
    return url  # déjà raw.githubusercontent.com


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        _check(newurl)  # re-valide la cible de CHAQUE redirection
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_skill_md(url: str) -> str:
    raw = resolve_github_raw(url)
    _check(raw)
    opener = urllib.request.build_opener(_AllowlistRedirect)
    req = urllib.request.Request(raw, headers={"User-Agent": "ghost-create"})
    try:
        with opener.open(req, timeout=20) as resp:
            _check(resp.geturl())  # ceinture + bretelles après redirections
            data: bytes = resp.read(_MAX_BYTES + 1)
    except CreateError:
        raise
    except Exception as exc:  # HTTPError (404…), URLError, réseau → message clair
        raise CreateError(f"récupération impossible : {exc}") from exc
    if len(data) > _MAX_BYTES:
        raise CreateError("fichier trop volumineux (> 512 KiB)")
    return data.decode("utf-8", "replace")


# ── Attribution : dépôt + license, lus depuis le dépôt lui-même ───────────────
def _owner_repo(url: str) -> tuple[str, str] | None:
    segs = [s for s in urlsplit(url).path.split("/") if s]
    if len(segs) < 2 or not _SEG_RE.match(segs[0]) or not _SEG_RE.match(segs[1]):
        return None
    repo = segs[1][:-4] if segs[1].endswith(".git") else segs[1]
    return segs[0], repo


def source_repo_from_url(url: str) -> str:
    owner_repo = _owner_repo(url)
    return f"https://github.com/{owner_repo[0]}/{owner_repo[1]}" if owner_repo else ""


def github_license_for_url(url: str, *, timeout: float = 8.0) -> str:
    """SPDX de la license du dépôt via l'API GitHub (best-effort). Hôte figé,
    owner/repo validés+encodés → pas de SSRF. "" (→ « unknown ») si indéterminée ;
    jamais inventée."""
    owner_repo = _owner_repo(url)
    if owner_repo is None:
        return ""
    owner, repo = owner_repo
    api = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/license"
    req = urllib.request.Request(
        api,
        headers={"User-Agent": "ghost-create", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return ""
            data = json.loads(resp.read(256 * 1024).decode("utf-8", "replace"))
    except Exception:  # best-effort : tout échec = license inconnue
        return ""
    lic = data.get("license") if isinstance(data, dict) else None
    spdx = str(lic.get("spdx_id") or "").strip() if isinstance(lic, dict) else ""
    return "" if spdx.upper() in ("", "NOASSERTION", "NONE") else spdx


# ── Normalisation LLM ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class NormalizedSkill:
    verdict: str  # 'SKILL' | 'SKIP'
    skip_reason: str
    name: str
    description: str
    tags: list[str]
    stack: list[str]
    signature: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


_SYSTEM = (
    "Tu normalises un skill Markdown arbitraire (hébergé sur GitHub) au format "
    "Ghost. Tu ne réécris PAS le contenu : tu génères UNIQUEMENT les métadonnées "
    "manquantes pour que le skill soit indexable et récupérable.\n\n"
    "Produis :\n"
    "- verdict : SKILL si c'est un vrai skill actionnable et spécifique (procédure, "
    "recette, piège concret, expertise réutilisable). SKIP si c'est générique ou du "
    "bruit : README marketing, doc d'installation banale, du CODE SOURCE brut (ce "
    "n'est pas un skill), conseils qu'un bon dev connaît déjà, page trop vague pour "
    "déclencher une récupération. SKIP est un succès, pas un échec.\n"
    "- skip_reason : si SKIP, la raison en une phrase ; sinon vide.\n"
    "- name : slug court en kebab-case, ASCII [a-z0-9-] uniquement.\n"
    "- description : UNE ligne orientée déclenchement (« Quand tu… » / « Use when… »), "
    "SANS deux-points. C'est LA clé de récupération : sans elle, le skill est "
    "introuvable.\n"
    "- tags : 2 à 5 tags courts en minuscules.\n"
    "- stack : la ou les technos principales, en liste courte (ex. [python], "
    "[typescript, react], [bash, git]), ou [general] si transverse.\n"
    "- signature : une task_signature au format EXACT « outils|exts|erreur|commit ». "
    "outils = outils/commandes principaux joints par « + » ; exts = extensions "
    "concernées jointes par « . » (ou « sans-fichier ») ; erreur = classe d'erreur ou "
    "symptôme en un mot (ou « sans-erreur ») ; commit = « commit » ou « sans-commit ». "
    "Exemple : « edit+bash-npm|ts.tsx|type-error|commit ». Déduis-la du contenu."
)

_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "verdict", "skip_reason", "name", "description", "tags", "stack", "signature",
    ],
    "properties": {
        "verdict": {"type": "string", "enum": ["SKILL", "SKIP"]},
        "skip_reason": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "stack": {"type": "array", "items": {"type": "string"}},
        "signature": {"type": "string"},
    },
}


def _strlist(value: object) -> list[str]:
    return [str(x).strip() for x in value if str(x).strip()] if isinstance(value, list) else []


def normalize_skill(raw_md: str, *, caller: LlmCaller) -> NormalizedSkill:
    """Markdown brut → NormalizedSkill (verdict + frontmatter généré). Lève
    CreateError sur tout échec récupérable (refus, tronqué, non parsable)."""
    user = (
        "Voici le contenu Markdown brut d'un skill trouvé sur GitHub.\n\n"
        f"{raw_md[:_MAX_INPUT_CHARS]}\n\n"
        "Génère le frontmatter Ghost, ou SKIP si le contenu est générique / du bruit / du code."
    )
    try:
        data, tin, tout = caller(_SYSTEM, user, _SCHEMA, _MAX_TOKENS)
    except DistillError as exc:
        raise CreateError(str(exc)) from exc
    verdict = "SKILL" if data.get("verdict") == "SKILL" else "SKIP"
    return NormalizedSkill(
        verdict=verdict,
        skip_reason=str(data.get("skip_reason") or "").strip(),
        name=str(data.get("name") or "").strip(),
        description=str(data.get("description") or "").strip(),
        tags=_strlist(data.get("tags")),
        stack=_strlist(data.get("stack")),
        signature=str(data.get("signature") or "").strip(),
        tokens_in=tin,
        tokens_out=tout,
        cost_usd=tin * PRICE_IN + tout * PRICE_OUT,
    )


# ── Rendu du SKILL.md (format distillé maison + provenance) ───────────────────
def base_slug(name: str) -> str:
    return (_SLUG_SPLIT.sub("-", name.lower()).strip("-") or "skill")[:64]


def _tag(t: str) -> str:
    return _SLUG_SPLIT.sub("-", str(t).lower()).strip("-")


def _one_line(desc: str) -> str:
    """Description sur une ligne, sans « : » (le format distillé n'échappe pas le
    frontmatter → un deux-points casserait le YAML)."""
    flat = " ".join(desc.split())
    return flat.replace(" : ", " — ").replace(": ", " — ").rstrip(":").strip()


def _strip_frontmatter(text: str) -> str:
    """Retire un éventuel frontmatter en tête (fence = ligne entière `---`)."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :]).strip()
    return text.strip()


def render_skill_md(
    norm: NormalizedSkill, *, source: str, license: str, raw_md: str
) -> str:
    """Frontmatter au format distillé (name/description/tags/stack en listes) +
    provenance (source/license) + corps d'origine préservé."""
    tags = [t for t in (_tag(x) for x in norm.tags) if t]
    stack = [s for s in (str(x).strip() for x in norm.stack) if s] or ["general"]
    body = _strip_frontmatter(raw_md)
    front = [
        "---",
        f"name: {base_slug(norm.name)}",
        f"description: {_one_line(norm.description)}",
        f"tags: [{', '.join(tags)}]",
        f"stack: [{', '.join(stack)}]",
        f"source: {source}",
        f"license: {license or 'unknown'}",
        "---",
        "",
    ]
    return "\n".join(front) + body + "\n"


# ── Persistance locale (candidates + skills + fichier) ────────────────────────
@dataclass(slots=True)
class CreatedSkill:
    slug: str
    skill_id: int
    candidate_id: int
    path: Path
    reimport: bool


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_local_skill(
    conn: Any, *, url: str, norm: NormalizedSkill, skill_md: str, skills_dir: Path
) -> CreatedSkill:
    """Écrit le skill importé comme un distillé maison : candidat `github_import`
    (status kept, task_signature = signature générée), skill SKILL, SKILL.md.
    Ré-import du même lien = nouvelle version, l'ancien skill désactivé."""
    now = _now()
    row = conn.execute(
        "SELECT id FROM candidates WHERE kind = 'github_import' AND signature = ?", (url,)
    ).fetchone()
    if row is not None:
        candidate_id, reimport = int(row[0]), True
        conn.execute(
            "UPDATE candidates SET status = 'kept', task_signature = ?, last_seen_at = ? "
            "WHERE id = ?",
            (norm.signature, now, candidate_id),
        )
        # Ré-import : l'ancien skill du candidat est désactivé (nouvelle version).
        conn.execute(
            "UPDATE skills SET disabled = 1 WHERE candidate_id = ? AND verdict = 'SKILL'",
            (candidate_id,),
        )
    else:
        cur = conn.execute(
            "INSERT INTO candidates (kind, signature, score, n_occ, n_sessions, "
            "session_ids_json, evidence_json, status, task_signature, created_at, "
            "last_seen_at) VALUES ('github_import', ?, 0, 0, 0, '[]', '[]', 'kept', "
            "?, ?, ?)",
            (url, norm.signature, now, now),
        )
        candidate_id, reimport = int(cur.lastrowid), False

    slug = _slugify(base_slug(norm.name), skills_dir)
    skill_dir = skills_dir / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(skill_md, encoding="utf-8")

    cur = conn.execute(
        "INSERT INTO skills (candidate_id, slug, path, model, prompt_version, verdict, "
        "low_value, disabled, skip_reason, critique_line, tokens_in, tokens_out, "
        "cost_usd, created_at) VALUES (?, ?, ?, ?, ?, 'SKILL', 0, 0, NULL, '', ?, ?, "
        "?, ?)",
        (
            candidate_id, slug, str(path), MODEL, PROMPT_VERSION,
            norm.tokens_in, norm.tokens_out, norm.cost_usd, now,
        ),
    )
    return CreatedSkill(
        slug=slug, skill_id=int(cur.lastrowid), candidate_id=candidate_id,
        path=path, reimport=reimport,
    )
