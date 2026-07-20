"""UI terminal de Ghost : une identité sobre, jamais bloquante, qui dégrade
proprement.

Règles dures (priment sur l'esthétique) :
- aucune animation n'ajoute de latence ni ne bloque une commande ; le travail
  réel tourne dans le corps du `with`, le spinner n'est qu'un reflet ;
- NO_COLOR, `--plain`, GHOST_PLAIN, ou absence de TTY (pipe, CI) → sortie plate,
  sans ANSI ni animation ; la **progression va sur stderr**, seul le résultat va
  sur stdout (un `ghost stats | cat` reste propre) ;
- dégrade sous 80 colonnes (logo compact ou masqué) ;
- interruptible : le spinner se démonte proprement sur Ctrl+C.

Palette : un accent (le bleu du logo), du gris pour le secondaire, vert/rouge
sobres pour succès/échec.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

ACCENT = "#7fb0ff"
MUTED = "grey58"
OK = "green"
BAD = "red3"
WARN = "yellow"

STATE_DIR = Path.home() / ".ghost"
_WELCOME_MARKER = STATE_DIR / ".welcomed"

_LOGO = ("╔═╗ ╦ ╦ ╔═╗ ╔═╗ ╔╦╗", "║ ╦ ╠═╣ ║ ║ ╚═╗  ║", "╚═╝ ╩ ╩ ╚═╝ ╚═╝  ╩")
_TAGLINE = "ghost · memory   —   measured, not upvoted"

_TRUE = {"1", "true", "yes", "on"}


def _env_plain() -> bool:
    if os.environ.get("NO_COLOR"):
        return True
    return os.environ.get("GHOST_PLAIN", "").strip().lower() in _TRUE


# `--plain` (callback) ou GHOST_PLAIN forcent une sortie totalement plate. NO_COLOR
# retire seulement la couleur (rich le gère nativement) : un spinner monochrome sur
# un vrai TTY reste acceptable, donc il n'entre pas dans _forced_plain.
_forced_plain = os.environ.get("GHOST_PLAIN", "").strip().lower() in _TRUE
_err: Console | None = None


def force_plain(value: bool = True) -> None:
    global _forced_plain
    _forced_plain = _forced_plain or value


def make_console() -> Console:
    """Console de résultats (stdout). rich respecte NO_COLOR et le non-TTY ;
    `--plain`/GHOST_PLAIN forcent no_color."""
    return Console(no_color=True if _forced_plain else None, highlight=False, soft_wrap=False)


def _err_console(base: Console) -> Console:
    global _err
    if _err is None:
        _err = Console(stderr=True, no_color=True if _forced_plain else None, highlight=False)
    return _err


def animate(console: Console) -> bool:
    """Vrai seulement sur un vrai terminal et hors mode plat forcé."""
    return console.is_terminal and not _forced_plain


def _logo_lines(color: bool) -> list[Text]:
    style = ACCENT if color else ""
    return [Text(row, style=style) for row in _LOGO]


# ── Accueil & version ────────────────────────────────────────────────────────
def render_logo(console: Console, *, tagline: bool = True) -> None:
    color = not console.no_color
    if console.width < 21:  # trop étroit : mot-repère minimal
        console.print(Text("ghost · memory", style=ACCENT if color else ""))
        return
    for line in _logo_lines(color):
        console.print(line)
    if tagline:
        console.print(Text(_TAGLINE, style=MUTED if color else ""))


def welcome(console: Console) -> None:
    """Écran d'accueil : logo, essence du produit, 3 premières actions. Sobre."""
    color = not console.no_color
    console.print()
    render_logo(console, tagline=False)
    console.print(
        Text("the collective memory for coding agents", style=MUTED if color else "")
    )
    console.print()
    for line in (
        "Your agent, powered by every developer's hard-won lessons.",
        "Skills ranked by what works — measured, not upvoted.",
        "Local by default. Nothing leaves your machine without you.",
    ):
        console.print("  " + line)
    console.print()
    console.print(Text("  Get started", style="bold"))
    for cmd, what in (
        ("ghost init", "set up, check your history, first scan"),
        ("ghost scan", "find the scars in your Claude Code history"),
        ("ghost login", "connect to the collective memory"),
    ):
        name = Text(f"    {cmd:<12}", style=ACCENT if color else "")
        console.print(name + Text(what, style=MUTED if color else ""))
    console.print()


def maybe_welcome(console: Console) -> None:
    """Affiche l'accueil UNE fois (premier lancement), et seulement sur un vrai
    terminal non plat — jamais dans un pipe/CI, jamais ré-affiché ensuite."""
    if not animate(console) or _WELCOME_MARKER.exists():
        return
    welcome(console)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _WELCOME_MARKER.write_text("", encoding="utf-8")
    except OSError:
        pass


def version_card(console: Console, version: str) -> None:
    color = not console.no_color
    if console.width < 40:
        render_logo(console, tagline=False)
        console.print(f"ghost memory  v{version}")
        return
    rows = _logo_lines(color)
    rows[1] = rows[1] + Text(f"   ghost memory  v{version}", style="bold" if color else "")
    rows[2] = rows[2] + Text("   https://ghostskills.com", style=MUTED if color else "")
    for r in rows:
        console.print(r)


# ── Progression « ça travaille » ─────────────────────────────────────────────
@contextmanager
def step(console: Console, message: str) -> Iterator[Callable[[str], None]]:
    """Spinner + ligne de statut décrivant l'étape. Le travail réel s'exécute
    dans le corps du `with` : aucune latence ajoutée. En mode plat/pipe, la
    progression part sur stderr (stdout reste propre)."""
    if animate(console):
        with console.status(
            Text(message, style=MUTED), spinner="dots", spinner_style=ACCENT
        ) as status:
            yield lambda msg: status.update(Text(msg, style=MUTED))
    else:
        err = _err_console(console)
        err.print(f"· {message}")
        # En mode plat, on ne spamme pas une ligne par sous-étape.
        yield lambda _msg: None


@contextmanager
def progress(
    console: Console, total: int, description: str
) -> Iterator[Callable[[int], None]]:
    """Barre de progression quand un total est connu (ingest, replay). Le corps
    fait le travail et appelle `advance(n)`. Dégrade en stderr hors TTY."""
    if animate(console) and total > 0:
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
        )

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(complete_style=ACCENT, finished_style=ACCENT),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as prog:
            task = prog.add_task(description, total=total)

            def _advance(n: int = 1) -> None:
                prog.advance(task, n)

            yield _advance
    else:
        err = _err_console(console)
        err.print(f"· {description} (0/{total})")

        def _noop(n: int = 1) -> None:
            return None

        yield _noop


def reveal(console: Console, blocks: Sequence[str], *, budget_s: float = 0.45) -> None:
    """Affiche des blocs avec un léger stagger (« il réfléchit »). Le délai total
    est plafonné (`budget_s`) : jamais de latence perçue. Instantané en mode plat."""
    live = animate(console)
    n = len(blocks)
    delay = min(0.04, budget_s / n) if (live and n) else 0.0
    for i, block in enumerate(blocks):
        console.print(block, highlight=False)
        if delay and i < n - 1:
            time.sleep(delay)


# ── Résumés, succès, erreurs ─────────────────────────────────────────────────
def summary(console: Console, title: str, body: str, *, style: str = ACCENT) -> None:
    """Résumé net encadré (rich Panel). En mode plat : un titre + le corps, sans
    cadre ANSI."""
    if console.no_color or not console.is_terminal:
        console.print(f"— {title} —")
        console.print(body)
        return
    console.print(
        Panel(
            body,
            title=Text(title, style="bold"),
            border_style=style,
            expand=False,
            padding=(0, 2),
        )
    )


def ok(console: Console, message: str) -> None:
    color = not console.no_color
    console.print(Text("✓ ", style=OK if color else "") + Text(message))


def fail(console: Console, cause: str, action: str | None = None) -> None:
    """Erreur claire : la cause + l'action à faire, jamais un stacktrace nu.
    Généralise le ton de `ghost doctor`. Va sur stderr."""
    err = _err_console(console)
    color = not err.no_color
    body = Text()
    body.append(cause)
    if action:
        body.append("\n→ ", style=ACCENT if color else "")
        body.append(action)
    if err.no_color or not err.is_terminal:
        err.print(f"✗ {cause}" + (f"\n→ {action}" if action else ""))
        return
    err.print(
        Panel(
            body,
            title=Text("✗ erreur", style=BAD),
            border_style=BAD,
            expand=False,
            padding=(0, 2),
        )
    )


_VERDICT = {
    "SKILL": OK,
    "SKIP": MUTED,
    "kept": ACCENT,
    "BUDGET": WARN,
    "ERREUR": BAD,
    "ERROR": BAD,
}


def verdict(text: str) -> Text:
    """Coloration cohérente des verdicts : SKILL vert, SKIP gris, kept accent."""
    return Text(text, style=_VERDICT.get(text, ""))
