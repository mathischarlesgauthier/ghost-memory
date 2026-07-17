"""Détecteurs de cicatrices — fonctions pures (db) -> list[Occurrence].

Seuils et règles calibrés sur le corpus réel (329 fenêtres jugées par un
panel de 84 agents, 2 lentilles indépendantes) :
- FAILURE_LOOP : ≥2 erreurs du même outil dans le même thread, après
  deny-list du bruit harness ; les runs de 1 sont exclus (précision 8 %).
- HUMAN_OVERRIDE : message humain dont le tour agent précédent contient
  ≥1 Edit/Write/Bash et qui matche les mots-clés FR/EN (précision 43 %).
  La mention de fichier seule est exclue (0 %). Pas de fenêtre temporelle
  (délai médian Edit→humain : 34 min en sessions autonomes).
- INTERRUPT : pas un détecteur (précision 7 %) — bonus de score.
- REPEATED_SEQUENCE : n-grammes 3..6 de tokens enrichis, ≥3 sessions.

Piège du corpus (445 tool_use_id présents dans plusieurs sessions) : les
sessions reprises/forkées REJOUENT l'historique dans un nouveau fichier.
D'où : jointures tool_use↔tool_result scopées au même src_file, et
déduplication des occurrences rejouées (par tool_use_id ou texte).
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

FAILURE_MIN_RUN = 2
SEQ_MIN_SESSIONS = 3
SEQ_NGRAM_RANGE = range(3, 7)

INTERRUPT_MARKER = "[Request interrupted"

HARNESS_NOISE: tuple[str, ...] = (
    "denied by the Claude Code auto mode classifier",
    "temporarily unavailable",
    "Blocked: sleep",
    "File has not been read yet",
    "The user doesn't want to proceed",
    INTERRUPT_MARKER,
)
"""Erreurs d'infrastructure du harness : neutres pour FAILURE_LOOP.
Les pièges d'outils (plafonds cachés, messages trompeurs) restent des
erreurs légitimes — c'est du skill vendable, pas du bruit."""

OVERRIDE_KEYWORDS: tuple[str, ...] = (
    "non", "pas", "plutôt", "en fait", "arrête", "arrete", "stop",
    "refais", "reviens", "annule", "corrige", "faux", "mauvais",
    "n'importe quoi", "pourquoi tu", "je t'ai dit", "c'est pas", "wrong",
    "don't", "revert", "undo", "instead", "actually", "not what", "soucis",
    "probleme", "problème", "bug",
)

OVERRIDE_TURN_TOOLS = frozenset({"Edit", "Write", "Bash"})


def _compile_keyword_matchers() -> list[tuple[str, re.Pattern[str] | None]]:
    """Mots simples → frontières de mots (\\b) : « bug » ne matche plus
    « debugger », « non » ne matche plus « sinon on ». Les locutions
    (espace ou apostrophe) restent en substring."""
    matchers: list[tuple[str, re.Pattern[str] | None]] = []
    for kw in OVERRIDE_KEYWORDS:
        if re.fullmatch(r"[\wéèêëàâûùôîïç-]+", kw):
            matchers.append((kw, re.compile(rf"\b{re.escape(kw)}\b")))
        else:
            matchers.append((kw, None))
    return matchers


_KEYWORD_MATCHERS = _compile_keyword_matchers()


def match_override_keywords(lower_text: str) -> list[str]:
    return [
        kw
        for kw, rx in _KEYWORD_MATCHERS
        if (rx.search(lower_text) if rx else kw in lower_text)
    ]


@dataclass(slots=True)
class Occurrence:
    """Une occurrence de cicatrice, avant fusion par signature."""

    kind: str
    signature: str
    session_id: str
    event_ids: list[int]
    cost: float
    ts: str | None
    count: int = 1
    meta: dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Normalisation


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_PATH_RE = re.compile(r"(?:/[\w.\-@~+]+){2,}")
_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_NUM_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")
_EXIT_LINE_RE = re.compile(r"^Exit code \d+$")
_KEY_LINE_RE = re.compile(
    r"error|exception|failed|fatal|traceback|refused|denied|not found|cannot|unable|missing",
    re.IGNORECASE,
)


def normalize_error(text: str) -> str:
    """Réduit un texte d'erreur à un motif stable : chemins, uuid, hashes,
    nombres et espaces normalisés. Prend la dernière ligne "significative"
    (celle d'un Traceback ou du message d'erreur), sinon la première."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _EXIT_LINE_RE.match(ln)]
    if not lines:
        return "<empty>"
    key = next((ln for ln in reversed(lines) if _KEY_LINE_RE.search(ln)), None)
    line = (key or lines[0])[:300]
    line = _UUID_RE.sub("<uuid>", line)
    line = _PATH_RE.sub("<path>", line)
    line = _HASH_RE.sub("<hash>", line)
    line = _NUM_RE.sub("<n>", line)
    line = _WS_RE.sub(" ", line)
    return line[:160]


_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WORD_RE = re.compile(r"^[a-zA-Z][\w.-]*$")
_MULTI_CMD = frozenset(
    {"git", "uv", "npm", "pnpm", "npx", "docker", "python", "python3", "uvx", "cargo", "make"}
)
_RUNNER_PAIRS = frozenset({("uv", "run"), ("npm", "run"), ("pnpm", "run"), ("npm", "exec")})


def bash_token(command: str) -> str:
    """Token enrichi pour REPEATED_SEQUENCE : `Bash:git-commit`,
    `Bash:uv-run-pytest`… Ignore les préfixes env, `cd X &&`, et les flags."""
    parts = command.strip().split()
    i = 0
    while i < len(parts):
        word = parts[i]
        if _ENV_RE.match(word):
            i += 1
            continue
        if word == "cd" and i + 2 < len(parts) and parts[i + 2] in ("&&", ";"):
            i += 3
            continue
        if word in ("&&", ";", "("):
            i += 1
            continue
        break
    if i >= len(parts):
        return "Bash"
    head = Path(parts[i]).name.lower()
    if head not in _MULTI_CMD:
        return f"Bash:{head}"
    j = i + 1
    while j < len(parts) and (parts[j].startswith("-") or _ENV_RE.match(parts[j])):
        j += 2 if parts[j] == "-C" else 1
    if j >= len(parts) or not _WORD_RE.match(parts[j]):
        return f"Bash:{head}"
    sub = parts[j].lower()
    if (head, sub) in _RUNNER_PAIRS:
        k = j + 1
        while k < len(parts) and parts[k].startswith("-"):
            k += 1
        if k < len(parts) and _WORD_RE.match(parts[k]):
            return f"Bash:{head}-{sub}-{Path(parts[k]).name.lower()}"
    return f"Bash:{head}-{sub}"


def _tool_input(payload_json: str | None) -> dict[str, object]:
    if not payload_json:
        return {}
    try:
        block = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    inp = block.get("input") if isinstance(block, dict) else None
    return inp if isinstance(inp, dict) else {}


def _is_harness_noise(text: str | None) -> bool:
    if not text:
        return False
    return any(marker in text for marker in HARNESS_NOISE)


# --------------------------------------------------------------------------
# FAILURE_LOOP


class _RunStep(NamedTuple):
    use_id: int
    result_id: int
    text: str
    ts: str | None
    tool_use_id: str


def detect_failure_loops(conn: sqlite3.Connection) -> list[Occurrence]:
    """≥2 erreurs consécutives du même outil (dans la sous-séquence de cet
    outil, autres outils interleavés autorisés), même thread. Le bruit
    harness est neutre : il n'étend ni ne casse un run. Un run dont la
    première erreur (tool_use_id) a déjà été vue pour la même signature est
    un transcript rejoué → ignoré."""
    cursor = conn.execute(
        """
        SELECT u.id, u.session_id, COALESCE(u.agent_id, ''), u.tool_name, u.ts,
               r.id, COALESCE(r.is_error, 0), r.text, u.tool_use_id
        FROM events u
        JOIN events r ON r.tool_use_id = u.tool_use_id
             AND r.block_type = 'tool_result'
             AND r.src_file = u.src_file
        WHERE u.block_type = 'tool_use' AND u.tool_name IS NOT NULL
        ORDER BY u.session_id, COALESCE(u.agent_id, ''), u.seq
        """
    )
    out: list[Occurrence] = []
    seen_first_error: dict[str, set[str]] = {}
    thread: tuple[str, str] | None = None
    runs: dict[str, list[_RunStep]] = {}

    def emit(tool: str, run: list[_RunStep], converged: bool, session_id: str,
             success_ids: list[int]) -> None:
        motif = normalize_error(run[-1].text)
        signature = f"{tool}|{motif}"
        seen = seen_first_error.setdefault(signature, set())
        if run[0].tool_use_id in seen:
            return
        seen.add(run[0].tool_use_id)
        event_ids = [i for step in run for i in (step.use_id, step.result_id)] + success_ids
        out.append(
            Occurrence(
                kind="FAILURE_LOOP",
                signature=signature,
                session_id=session_id,
                event_ids=event_ids,
                cost=float(len(run)),
                ts=run[-1].ts,
                meta={"tool": tool, "motif": motif, "converged": converged,
                      "n_errors": len(run)},
            )
        )

    def flush(session_id: str) -> None:
        for tool, run in runs.items():
            if len(run) >= FAILURE_MIN_RUN:
                emit(tool, run, False, session_id, [])
        runs.clear()

    for use_id, session_id, agent_id, tool, ts, res_id, is_error, text, tuid in cursor:
        key = (session_id, agent_id)
        if key != thread:
            if thread is not None:
                flush(thread[0])
            thread = key
        if is_error and _is_harness_noise(text):
            continue
        run = runs.setdefault(tool, [])
        if is_error:
            run.append(_RunStep(use_id, res_id, text or "", ts, tuid or ""))
        else:
            if len(run) >= FAILURE_MIN_RUN:
                emit(tool, run, True, session_id, [use_id, res_id])
            runs[tool] = []
    if thread is not None:
        flush(thread[0])
    return out


# --------------------------------------------------------------------------
# HUMAN_OVERRIDE


class _Ev(NamedTuple):
    session_id: str
    id: int
    ts: str | None
    block_type: str
    tool_name: str | None
    is_human: int
    text: str | None
    payload: str | None


def detect_human_overrides(conn: sqlite3.Connection) -> list[Occurrence]:
    """Message humain (thread principal) précédé d'un tour agent contenant
    ≥1 Edit/Write/Bash, et matchant les mots-clés de correction FR/EN.
    Un message humain identique déjà émis (session reprise rejouant
    l'historique) n'est compté qu'une fois."""
    projects = {
        str(sid): str(proj) for sid, proj in conn.execute("SELECT id, project FROM sessions")
    }
    cursor = conn.execute(
        """
        SELECT session_id, id, ts, block_type, tool_name, is_human, text, payload_json
        FROM events WHERE agent_id IS NULL
        ORDER BY session_id, seq
        """
    )
    out: list[Occurrence] = []
    seen_texts: set[str] = set()
    for session_id, group in itertools.groupby(cursor, key=lambda r: str(r[0])):
        evs = [_Ev._make(r) for r in group]
        prev_human = -1
        for i, e in enumerate(evs):
            if not e.is_human or (e.text or "").startswith(INTERRUPT_MARKER):
                continue
            turn = evs[prev_human + 1 : i]
            prev_human = i
            tool_uses = [t for t in turn if t.block_type == "tool_use"]
            if not any(t.tool_name in OVERRIDE_TURN_TOOLS for t in tool_uses):
                continue
            lower = (e.text or "").lower()
            keywords = match_override_keywords(lower)
            if not keywords:
                continue
            dedup_key = (e.text or "")[:200]
            if dedup_key in seen_texts:
                continue
            seen_texts.add(dedup_key)
            edits = [t for t in tool_uses if t.tool_name in ("Edit", "Write")]
            short_path = _turn_focus(edits, tool_uses)
            interrupted = any(
                (t.text or "").startswith(INTERRUPT_MARKER)
                for t in turn + evs[i + 1 : i + 4]
            )
            out.append(
                Occurrence(
                    kind="HUMAN_OVERRIDE",
                    signature=f"{projects.get(session_id, '?')}|{short_path}",
                    session_id=session_id,
                    event_ids=[e.id, *[t.id for t in edits][-8:]],
                    cost=float(len(edits)),
                    ts=e.ts,
                    meta={
                        "keywords": keywords[:6],
                        "file": short_path,
                        "interrupt": interrupted,
                        "excerpt": (e.text or "")[:220],
                    },
                )
            )
    return out


def _turn_focus(edits: list[_Ev], tool_uses: list[_Ev]) -> str:
    """Fichier le plus édité du tour ; à défaut (tour Bash-only), la
    commande Bash dominante — jamais '<none>' fusionnant tout un projet."""
    paths = [
        str(fp)
        for t in edits
        if isinstance(fp := _tool_input(t.payload).get("file_path"), str) and fp
    ]
    if paths:
        top = Counter(paths).most_common(1)[0][0]
        return "/".join(Path(top).parts[-2:])
    commands = [
        bash_token(cmd)
        for t in tool_uses
        if t.tool_name == "Bash"
        and isinstance(cmd := _tool_input(t.payload).get("command"), str)
    ]
    if commands:
        return Counter(commands).most_common(1)[0][0]
    return "<none>"


# --------------------------------------------------------------------------
# REPEATED_SEQUENCE


class _SessionGramStats:
    """Par (n-gramme, session) : ids de la première fenêtre, ts max, count."""

    __slots__ = ("count", "first_ids", "max_ts")

    def __init__(self, first_ids: list[int], ts: str | None) -> None:
        self.first_ids = first_ids
        self.max_ts = ts
        self.count = 1

    def add(self, ts: str | None) -> None:
        self.count += 1
        if ts is not None and (self.max_ts is None or ts > self.max_ts):
            self.max_ts = ts


def _contains_subsequence(long: tuple[str, ...], short: tuple[str, ...]) -> bool:
    """Sous-séquence CONTIGUË de tokens (pas de substring sur la chaîne
    jointe : 'Bash:uv-run' n'est pas contenu dans 'Bash:uv-run-pytest')."""
    n = len(short)
    return any(long[i : i + n] == short for i in range(len(long) - n + 1))


def detect_repeated_sequences(conn: sqlite3.Connection) -> list[Occurrence]:
    """N-grammes (3..6) de tokens d'outils enrichis présents dans ≥3
    sessions distinctes. Une fenêtre dont les tool_use_id ont déjà été vus
    (transcript rejoué par une session reprise) n'est pas recomptée. Une
    occurrence émise par session et par signature (count = nb de fenêtres)."""
    cursor = conn.execute(
        """
        SELECT id, session_id, COALESCE(agent_id, ''), tool_name, ts, payload_json,
               COALESCE(tool_use_id, '')
        FROM events WHERE block_type = 'tool_use' AND tool_name IS NOT NULL
        ORDER BY session_id, COALESCE(agent_id, ''), seq
        """
    )
    threads: dict[tuple[str, str], list[tuple[str, int, str | None, str]]] = {}
    for eid, session_id, agent_id, tool, ts, payload, tuid in cursor:
        if tool == "Bash":
            command = _tool_input(payload).get("command")
            token = bash_token(command) if isinstance(command, str) else "Bash"
        else:
            token = str(tool)
        threads.setdefault((session_id, agent_id), []).append((token, eid, ts, tuid))

    grams: dict[tuple[str, ...], dict[str, _SessionGramStats]] = {}
    seen_windows: set[tuple[str, ...]] = set()
    for (session_id, _agent), seq in threads.items():
        for n in SEQ_NGRAM_RANGE:
            for start in range(len(seq) - n + 1):
                window = seq[start : start + n]
                replay_key = tuple(tuid for _, _, _, tuid in window)
                if all(replay_key) and replay_key in seen_windows:
                    continue
                if all(replay_key):
                    seen_windows.add(replay_key)
                key = tuple(t for t, _, _, _ in window)
                per_session = grams.setdefault(key, {})
                stats = per_session.get(session_id)
                if stats is None:
                    per_session[session_id] = _SessionGramStats(
                        [eid for _, eid, _, _ in window], window[-1][2]
                    )
                else:
                    stats.add(window[-1][2])

    kept = {
        key: sessions
        for key, sessions in grams.items()
        if len(sessions) >= SEQ_MIN_SESSIONS and not _is_stopgram(key)
    }
    # Élagage 1 : un n-gramme contenu (sous-séquence de tokens) dans un plus
    # long gardé, avec un volume comparable (>=90 %), est redondant.
    occ_count = {
        key: sum(s.count for s in sessions.values()) for key, sessions in kept.items()
    }
    final: list[tuple[str, ...]] = []
    for key in sorted(kept, key=len, reverse=True):
        redundant = any(
            len(other) > len(key)
            and _contains_subsequence(other, key)
            and occ_count[other] >= 0.9 * occ_count[key]
            for other in final
        )
        if not redundant:
            final.append(key)
    # Élagage 2 : les variantes décalées (même multiset de tokens) sont un
    # seul motif — on garde la plus fréquente.
    best_by_multiset: dict[tuple[str, ...], tuple[str, ...]] = {}
    for key in final:
        mkey = tuple(sorted(key))
        cur = best_by_multiset.get(mkey)
        if cur is None or occ_count[key] > occ_count[cur]:
            best_by_multiset[mkey] = key

    out: list[Occurrence] = []
    for key in best_by_multiset.values():
        signature = "→".join(key)
        for session_id, stats in kept[key].items():
            out.append(
                Occurrence(
                    kind="REPEATED_SEQUENCE",
                    signature=signature,
                    session_id=session_id,
                    event_ids=stats.first_ids,
                    cost=0.0,
                    ts=stats.max_ts,
                    count=stats.count,
                    meta={"tokens": list(key)},
                )
            )
    return out


EXPLORATION_TOKENS = frozenset(
    {
        "Read", "Glob", "Grep", "ToolSearch", "Bash",
        "Bash:grep", "Bash:rg", "Bash:find", "Bash:ls", "Bash:cat", "Bash:head",
        "Bash:tail", "Bash:echo", "Bash:wc", "Bash:sed", "Bash:awk", "Bash:pwd",
        "Bash:cd", "Bash:sleep", "Bash:true", "Bash:test",
        "Bash:which", "Bash:du", "Bash:df", "Bash:git-status", "Bash:git-log",
        "Bash:git-diff", "Bash:tree", "Bash:file", "Bash:stat",
    }
)
"""Tokens de pure exploration : un n-gramme qui n'en sort pas est le
comportement générique de n'importe quel agent — sans valeur (vérifié :
sans ce filtre, le top-15 réel est 100 % de churn grep/Read)."""


def _is_stopgram(key: tuple[str, ...]) -> bool:
    distinct = set(key)
    if len(distinct) == 1:
        return True
    if distinct <= EXPLORATION_TOKENS:
        return True
    return not (any(":" in t for t in key) or len(distinct) >= 3)


# --------------------------------------------------------------------------
# GROUND_TRUTH


GIT_COMMIT_RE = re.compile(r"\bgit\b[^|;&]*\bcommit\b")


def commit_timestamps(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Timestamps des `git commit` réussis, par session (triés). Le
    classifieur lit la COMMANDE parsée (pas le payload brut) : `git -C x
    commit` est détecté, une description qui mentionne « git commit » non."""
    out: dict[str, list[str]] = {}
    for session_id, ts, payload in conn.execute(
        """
        SELECT u.session_id, r.ts, u.payload_json
        FROM events u
        JOIN events r ON r.tool_use_id = u.tool_use_id AND r.block_type = 'tool_result'
             AND r.src_file = u.src_file AND COALESCE(r.is_error, 0) = 0
        WHERE u.block_type = 'tool_use' AND u.tool_name = 'Bash'
              AND u.payload_json LIKE '%commit%' AND r.ts IS NOT NULL
        ORDER BY r.ts
        """
    ):
        command = _tool_input(payload).get("command")
        if isinstance(command, str) and GIT_COMMIT_RE.search(command):
            out.setdefault(session_id, []).append(ts)
    return out


def tool_input(payload_json: str | None) -> Mapping[str, object]:
    """Accès public à l'input d'un tool_use depuis son payload_json."""
    return _tool_input(payload_json)
