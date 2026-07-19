"""Commandes de compte côté CLI : usage, unlocked, earnings, account, history,
whoami, payout-setup. Lisent le VRAI état via l'API (jamais un chiffre inventé),
gèrent Free / offline (dernier état connu) / données vides sans jamais planter.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from rich.console import Console

from ghost.network import (
    NetworkError,
    api_get,
    api_post,
    load_token,
    read_cache,
    write_cache,
)

console = Console()


class _Stop(Exception):
    def __init__(self, code: int, msg: str | None = None) -> None:
        self.code = code
        self.msg = msg


def run(fn: Callable[[], None]) -> int:
    """Exécute une commande, mappe _Stop → (message, code de sortie)."""
    try:
        fn()
    except _Stop as stop:
        if stop.msg:
            console.print(stop.msg)
        return stop.code
    return 0


def _fetch(path: str, cache_name: str) -> tuple[dict[str, Any], bool]:
    """(data, stale). Offline → dernier état connu + stale=True ; sans cache →
    message honnête et sortie 0 (jamais un plantage)."""
    token = load_token()
    if not token:
        raise _Stop(1, "connecte-toi d'abord : [bold]ghost login[/bold]")
    try:
        status, data = api_get(path, token)
    except NetworkError:
        cached = read_cache(cache_name)
        if cached is None:
            raise _Stop(
                0, "[yellow]hors ligne[/yellow] — réseau injoignable, aucun état en cache."
            ) from None
        return cached, True
    if status == 401:
        raise _Stop(1, "jeton invalide ou expiré — relance [bold]ghost login[/bold].")
    if status != 200:
        raise _Stop(1, f"[red]erreur {status}[/red] : {data.get('detail')}")
    write_cache(cache_name, data)
    return data, False


def _stale_banner(stale: bool) -> None:
    if stale:
        console.print(
            "[yellow]⚠ hors ligne — dernier état connu, peut être périmé.[/yellow]\n"
        )


def _bar(pct: float, width: int = 22) -> str:
    filled = round(width * min(pct, 100.0) / 100.0)
    return "█" * filled + "░" * (width - filled)


def _euros(cents: int) -> str:
    return f"€{cents / 100:.2f}"


# --------------------------------------------------------------------------
# Commandes


def usage() -> None:
    data, stale = _fetch("/account/usage", "usage")
    _stale_banner(stale)
    tier = str(data.get("tier", "free"))
    used = int(data.get("unlocks_used", 0))
    quota = int(data.get("unlocks_quota", 0))
    remaining = int(data.get("unlocks_remaining", 0))
    pct = float(data.get("pct_used", 0.0))
    resets = data.get("resets_on")
    lifetime = tier == "free"
    scope = "lifetime" if lifetime else "this cycle"
    console.print(f"[bold]{tier.capitalize()}[/bold] plan")
    console.print(
        f"  community unlocks: [bold]{used}[/bold] / {quota} {scope}   "
        f"[dim]{_bar(pct)}[/dim] {pct:.0f}%"
    )
    console.print(f"  remaining: {remaining}")
    if resets:
        console.print(f"  resets on: {resets}")
    elif lifetime:
        console.print("  no monthly reset — the 5 free unlocks are one-time, to try")
    if not lifetime and pct >= 80:
        console.print(
            f"\n[yellow]You've used {pct:.0f}% of your quota.[/yellow] "
            "Upgrade for more: [bold]ghost upgrade team[/bold]."
        )
    if lifetime and used >= quota:
        console.print(
            "\nOut of free unlocks. [bold]ghost upgrade pro[/bold] for 200/month."
        )


def unlocked() -> None:
    data, stale = _fetch("/account/unlocked", "unlocked")
    _stale_banner(stale)
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        console.print(
            "No community skills unlocked this cycle. Retrieve pulls them in as you "
            "code; each distinct one counts as an unlock."
        )
        return
    console.print(f"[bold]{len(items)}[/bold] community skill(s) unlocked this cycle\n")
    for it in items:
        lift = it.get("lift")
        lift_s = (
            f"[bold]{float(lift) * 100:+.0f}%[/bold]"
            if isinstance(lift, (int, float))
            else "[dim]lift not yet measured[/dim]"
        )
        author = it.get("author") or "—"
        console.print(
            f"  {it.get('slug', '')!s:<34} {lift_s}   [dim]@{author}[/dim]"
        )


def earnings() -> None:
    data, stale = _fetch("/account/earnings", "earnings")
    _stale_banner(stale)
    balance = int(data.get("balance_cents", 0))
    threshold = int(data.get("threshold_cents", 5000))
    share = float(data.get("impact_share_pct", 0.0))
    installs = int(data.get("installs_generated", 0))
    avg_lift = data.get("avg_lift")
    configured = bool(data.get("payout_configured", False))
    has_earned = bool(data.get("has_earned", False))

    console.print(
        "[bold]Earnings[/bold]  "
        "[dim](50% of subscriptions, paid for lift x adoption)[/dim]"
    )
    console.print(f"  balance: [bold]{_euros(balance)}[/bold]")
    if not has_earned:
        console.print(
            "  [dim]your skills haven't earned yet — payouts scale with measured lift "
            "as the network grows[/dim]"
        )
    console.print(f"  measured impact share this cycle: {share:.2f}%")
    console.print(f"  installs your skills generated: {installs}")
    if isinstance(avg_lift, (int, float)):
        console.print(f"  avg measured lift of your skills: {float(avg_lift) * 100:+.0f}%")
    else:
        console.print("  avg measured lift: [dim]not yet measured[/dim]")
    to_go = max(0, threshold - balance)
    console.print(
        f"  payout threshold: {_euros(balance)} / {_euros(threshold)}"
        + (f"  [dim]— {_euros(to_go)} to go[/dim]" if to_go > 0 else "  [green]— reached[/green]")
    )
    if configured:
        console.print("  payout details: [green]configured[/green]")
    else:
        console.print(
            "  payout details: not set — [bold]ghost payout-setup[/bold] "
            "[dim](optional, only needed to cash out)[/dim]"
        )


def history() -> None:
    data, stale = _fetch("/account/history", "history")
    _stale_banner(stale)
    rows = data.get("_list") if isinstance(data, dict) and "_list" in data else data
    rows = rows if isinstance(rows, list) else []
    if not rows:
        console.print("No payouts yet. When your balance passes the threshold, monthly "
                      "payouts appear here.")
        return
    console.print("[bold]Payout history[/bold]\n")
    for p in rows:
        console.print(
            f"  {p.get('created_at', '')[:10]}  {_euros(int(p.get('amount_cents', 0)))}"
            f"  [dim]{p.get('status', '')}[/dim]"
        )


def account() -> None:
    data, stale = _fetch("/account", "account")
    _stale_banner(stale)
    tier = str(data.get("tier", "free"))
    used = int(data.get("unlocks_used", 0))
    quota = int(data.get("unlocks_quota", 0))
    balance = int(data.get("balance_cents", 0))
    threshold = int(data.get("threshold_cents", 5000))
    email = data.get("email") or "[dim]not set[/dim]"
    resets = data.get("resets_on")
    configured = bool(data.get("payout_configured", False))
    profile = data.get("profile_url")

    console.print("[bold]Ghost Skills — account[/bold]")
    console.print(f"  plan:     [bold]{tier.capitalize()}[/bold]"
                  + (f"   resets {resets}" if resets else "   (lifetime)"))
    console.print(f"  email:    {email}")
    console.print(f"  usage:    {used} / {quota} community unlocks")
    console.print(
        f"  earnings: {_euros(balance)}  "
        f"[dim](threshold {_euros(threshold)}"
        f"{', payouts on' if configured else ', payouts off'})[/dim]"
    )
    if profile:
        console.print(f"  profile:  {profile}")
    console.print("\n[dim]details: ghost usage · ghost unlocked · ghost earnings[/dim]")
    if tier == "free":
        console.print(
            "\n[dim]Pro ($29/mo) unlocks 200 community skills/month, sync, and the "
            "contributor revenue share. → ghost upgrade pro[/dim]"
        )


def whoami() -> None:
    data, stale = _fetch("/account", "account")
    _stale_banner(stale)
    tier = str(data.get("tier", "free"))
    who = data.get("email") or data.get("handle") or "anonymous"
    console.print(f"{who} · [bold]{tier}[/bold]")


def payout_setup() -> None:
    token = load_token()
    if not token:
        raise _Stop(1, "connecte-toi d'abord : [bold]ghost login[/bold]")
    try:
        status, data = api_post("/account/payout-link", token)
    except NetworkError as exc:
        raise _Stop(1, f"[red]{exc}[/red]") from exc
    if status != 200:
        raise _Stop(1, f"[red]erreur {status}[/red] : {data.get('detail')}")
    url = str(data.get("url", ""))
    console.print(
        "Payout setup is [bold]optional[/bold] — only needed to cash out, not to "
        "contribute or build reputation.\nNo bank details ever pass through the "
        "terminal; opening a secure page:"
    )
    console.print(f"  [bold]{url}[/bold]")
    with suppress(Exception):
        webbrowser.open(url)
