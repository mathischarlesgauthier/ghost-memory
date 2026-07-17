"""Replay sandboxé d'une tâche historique — défensif partout.

Sandbox : `git clone --local` vers un tmpdir, checkout détaché du commit
parent, remote retiré (hygiène, PAS une frontière : la revue adversariale
a prouvé qu'un `git push <chemin>` joint un repo par chemin absolu sans
remote). La vraie frontière est le modèle de permissions INVERSÉ : seuls
des préfixes de commandes précis sont autorisés, tout le reste — push,
curl, rm, sudo… — est refusé par défaut en mode -p. Environnement minimal
(aucun secret du shell parent), kill du groupe de processus au timeout,
cleanup try/finally même sur crash.

Risque résiduel assumé et documenté : un agent malveillant pourrait abuser
d'un préfixe autorisé (ex. `uv run python -c …`). Les replays tournent sur
les prompts historiques de l'utilisateur + ses propres skills — pas sur des
entrées hostiles — et sans secrets dans l'environnement.

Run : config Claude Code VIERGE (CLAUDE_CONFIG_DIR temporaire) + clé API —
le seul montage qui isole les skills globaux tout en gardant l'auth
(mesuré : --bare et config vierge sans clé = "Not logged in"). Le diff
entre conditions est donc UNIQUEMENT le skill injecté dans
`.claude/skills/` du sandbox. Même modèle des deux côtés.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from ghost.deploy import convert_for_claude_code

API_KEY_FILE = Path.home() / ".ghost" / "api_key"
REPLAY_MODEL = "claude-sonnet-5"
RUN_TIMEOUT_S = 900
PER_RUN_BUDGET_USD = 0.60

# Modèle inversé : seuls ces préfixes sont autorisés ; push, curl, rm,
# sudo et tout le reste sont refusés par défaut (permission_denial en -p).
ALLOWED_TOOLS: tuple[str, ...] = (
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)",
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(uv run:*)", "Bash(uv sync:*)", "Bash(pytest:*)",
    "Bash(npm run:*)", "Bash(npm test:*)", "Bash(pnpm run:*)",
    "Bash(pnpm test:*)", "Bash(ls:*)", "Bash(mkdir:*)", "Bash(grep:*)",
    "Bash(find:*)", "Bash(cat:*)",
)

# Environnement minimal : AUCUN secret du shell parent ne passe au replay.
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL")


class ReplayError(RuntimeError):
    pass


def _as_int(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _as_float(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


@dataclass(slots=True)
class RunMetrics:
    turns: int
    cost_usd: float
    duration_ms: int
    output_tokens: int
    tool_errors: int
    new_commits: int
    changed_lines: int
    success: bool
    is_error: bool
    denials: int
    timed_out: bool = False
    jsonl_missing: bool = False  # transcript introuvable : tool_errors invalide


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise ReplayError(f"git {' '.join(args)}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def commit_exists(repo: Path, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode == 0


@contextmanager
def sandbox(repo: Path, base_commit: str) -> Iterator[Path]:
    """Clone local détaché sur base_commit, sans remote. Jamais le repo de
    travail. Cleanup garanti."""
    if not (repo / ".git").exists():
        raise ReplayError(f"{repo} n'est pas un repo git")
    if not commit_exists(repo, base_commit):
        raise ReplayError(f"commit {base_commit} introuvable dans {repo}")
    tmp = Path(tempfile.mkdtemp(prefix="ghost-replay-"))
    try:
        work = tmp / "repo"
        proc = subprocess.run(
            ["git", "clone", "--local", "--no-hardlinks", "--quiet",
             str(repo), str(work)],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise ReplayError(f"clone: {proc.stderr.strip()[:200]}")
        _git(work, "checkout", "--detach", "--quiet", base_commit)
        remotes = _git(work, "remote")
        for remote in remotes.splitlines():
            if remote.strip():
                _git(work, "remote", "remove", remote.strip())
        yield work
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def inject_skill(work: Path, skill_source: Path, slug: str) -> None:
    target = work / ".claude" / "skills" / slug
    target.mkdir(parents=True, exist_ok=True)
    content = convert_for_claude_code(skill_source.read_text(encoding="utf-8"))
    (target / "SKILL.md").write_text(content, encoding="utf-8")


def _count_tool_errors(cfg_dir: Path) -> tuple[int, bool]:
    """(nb de tool_result en erreur, transcript trouvé ?). « Pas de JSONL »
    n'est PAS « pas d'erreur » : le second membre distingue les deux."""
    n = 0
    files_found = False
    for jsonl in cfg_dir.glob("projects/*/*.jsonl"):
        files_found = True
        try:
            with open(jsonl, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"is_error":true' not in line and '"is_error": true' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = obj.get("message")
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content")
                    if isinstance(content, list):
                        n += sum(
                            1
                            for b in content
                            if isinstance(b, dict)
                            and b.get("type") == "tool_result"
                            and b.get("is_error") is True
                        )
        except OSError:
            continue
    return n, files_found


COMMIT_INSTRUCTION = (
    "\n\nIMPORTANT : quand la tâche est terminée (ou au mieux de ce que tu "
    "peux faire), committe ton travail avec git add + git commit. Ne pushe pas."
)
"""Ajoutée aux DEUX conditions : rend le critère de succès (commit produit)
comparable même quand le prompt historique ne demandait le commit qu'à un
tour humain ultérieur (biais du replay mono-tour, documenté)."""


def run_replay(
    work: Path,
    prompt: str,
    *,
    budget_usd: float = PER_RUN_BUDGET_USD,
    model: str = REPLAY_MODEL,
    timeout_s: int = RUN_TIMEOUT_S,
) -> RunMetrics:
    api_key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    cfg = Path(tempfile.mkdtemp(prefix="ghost-replay-cfg-"))
    try:
        base_commit = _git(work, "rev-parse", "HEAD")
        base_commits = int(_git(work, "rev-list", "--count", "HEAD"))
        env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
        env["CLAUDE_CONFIG_DIR"] = str(cfg)
        env["ANTHROPIC_API_KEY"] = api_key
        cmd = [
            "claude", "-p", prompt + COMMIT_INSTRUCTION,
            "--output-format", "json",
            "--model", model,
            "--max-budget-usd", str(budget_usd),
            "--permission-mode", "acceptEdits",
            "--allowedTools", *ALLOWED_TOOLS,
        ]
        proc = subprocess.Popen(
            cmd, cwd=work, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            stdout, _stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=30)
            tool_errors, found = _count_tool_errors(cfg)
            return RunMetrics(
                turns=0, cost_usd=budget_usd, duration_ms=timeout_s * 1000,
                output_tokens=0, tool_errors=tool_errors,
                new_commits=0, changed_lines=0, success=False, is_error=True,
                denials=0, timed_out=True, jsonl_missing=not found,
            )
        finally:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        result = _parse_result(stdout)
        new_commits = int(_git(work, "rev-list", "--count", "HEAD")) - base_commits
        # Travail produit = diff du commit de base à l'état final (commits
        # inclus) — le diff vs HEAD vaut 0 dès que l'agent committe.
        shortstat = ""
        with suppress(ReplayError):
            shortstat = _git(work, "diff", "--shortstat", base_commit)
        usage = result.get("usage")
        output_tokens = (
            usage.get("output_tokens") if isinstance(usage, dict) else 0
        )
        denials = result.get("permission_denials")
        tool_errors, found = _count_tool_errors(cfg)
        return RunMetrics(
            turns=_as_int(result.get("num_turns")),
            cost_usd=_as_float(result.get("total_cost_usd")),
            duration_ms=_as_int(result.get("duration_ms")),
            output_tokens=_as_int(output_tokens),
            tool_errors=tool_errors,
            new_commits=new_commits,
            changed_lines=_changed_lines(shortstat),
            success=new_commits > 0,
            is_error=bool(result.get("is_error")),
            denials=len(denials) if isinstance(denials, list) else 0,
            jsonl_missing=not found,
        )
    finally:
        shutil.rmtree(cfg, ignore_errors=True)


def _parse_result(stdout: str) -> dict[str, object]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"sortie claude non parsable: {stdout[:200]}") from exc
    if isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
        raise ReplayError("aucun event result dans la sortie claude")
    if isinstance(data, dict):
        return data
    raise ReplayError("sortie claude inattendue")


def _changed_lines(shortstat: str) -> int:
    total = 0
    for part in shortstat.split(","):
        digits = "".join(c for c in part if c.isdigit())
        if digits and ("insertion" in part or "deletion" in part):
            total += int(digits)
    return total
