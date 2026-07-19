"""Lot H (CLI) : commandes de compte — fetch, cache offline, rendu sans crash."""

from __future__ import annotations

from typing import Any

import pytest

from ghost import account as acct
from ghost.network import NetworkError

FREE = {
    "/account/usage": {
        "tier": "free", "unlocks_used": 0, "unlocks_quota": 5, "unlocks_remaining": 5,
        "overage_units": 0, "period": "lifetime", "resets_on": None, "pct_used": 0.0,
        "active": True,
    },
    "/account/unlocked": {"period": "lifetime", "items": []},
    "/account/earnings": {
        "balance_cents": 0, "impact_share_pct": 0.0, "installs_generated": 0,
        "avg_lift": None, "n_measured_skills": 0, "n_public_skills": 0,
        "threshold_cents": 5000, "payout_configured": False, "pool_cents": 0,
        "has_earned": False,
    },
    "/account": {
        "handle": "alice", "email": None, "tier": "free", "active": True,
        "period": "lifetime", "resets_on": None, "unlocks_used": 0, "unlocks_quota": 5,
        "balance_cents": 0, "threshold_cents": 5000, "payout_configured": False,
        "profile_url": "https://ghost-memory.com/@alice",
    },
    "/account/history": [],
}


def _mock_ok(monkeypatch: pytest.MonkeyPatch, table: dict[str, Any]) -> None:
    monkeypatch.setattr(acct, "load_token", lambda: "tok")
    monkeypatch.setattr(acct, "write_cache", lambda _n, _d: None)
    monkeypatch.setattr(acct, "api_get", lambda p, _t: (200, table[p]))


def test_euros_and_bar() -> None:
    assert acct._euros(5000) == "€50.00"
    assert acct._euros(3200) == "€32.00"
    assert len(acct._bar(50)) == len(acct._bar(0)) == 22


def test_fetch_success_writes_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acct, "load_token", lambda: "tok")
    monkeypatch.setattr(acct, "api_get", lambda _p, _t: (200, {"tier": "pro"}))
    saved: dict[str, Any] = {}
    monkeypatch.setattr(acct, "write_cache", lambda n, d: saved.update({n: d}))
    data, stale = acct._fetch("/account/usage", "usage")
    assert data == {"tier": "pro"} and stale is False and saved["usage"] == {"tier": "pro"}


def test_fetch_offline_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acct, "load_token", lambda: "tok")

    def boom(_p: str, _t: str) -> tuple[int, dict[str, Any]]:
        raise NetworkError("down")

    monkeypatch.setattr(acct, "api_get", boom)
    monkeypatch.setattr(acct, "read_cache", lambda _n: {"tier": "pro"})
    data, stale = acct._fetch("/x", "usage")
    assert stale is True and data == {"tier": "pro"}


def test_fetch_offline_no_cache_stops_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acct, "load_token", lambda: "tok")

    def boom(_p: str, _t: str) -> tuple[int, dict[str, Any]]:
        raise NetworkError("down")

    monkeypatch.setattr(acct, "api_get", boom)
    monkeypatch.setattr(acct, "read_cache", lambda _n: None)
    with pytest.raises(acct._Stop) as e:
        acct._fetch("/x", "usage")
    assert e.value.code == 0  # jamais un plantage


def test_fetch_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acct, "load_token", lambda: None)
    with pytest.raises(acct._Stop) as e:
        acct._fetch("/x", "usage")
    assert e.value.code == 1


def test_fetch_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acct, "load_token", lambda: "tok")
    monkeypatch.setattr(acct, "api_get", lambda _p, _t: (401, {"detail": "nope"}))
    with pytest.raises(acct._Stop) as e:
        acct._fetch("/x", "usage")
    assert e.value.code == 1


def test_all_read_commands_render_free_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_ok(monkeypatch, FREE)
    for fn in (acct.usage, acct.unlocked, acct.earnings, acct.account, acct.history, acct.whoami):
        assert acct.run(fn) == 0  # aucun ne plante, aucun chiffre inventé


def test_commands_offline_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acct, "load_token", lambda: "tok")

    def boom(_p: str, _t: str) -> tuple[int, dict[str, Any]]:
        raise NetworkError("down")

    monkeypatch.setattr(acct, "api_get", boom)
    monkeypatch.setattr(acct, "read_cache", lambda _n: FREE["/account"])
    assert acct.run(acct.account) == 0  # dégradé, jamais un mur d'erreur


def test_payout_setup_opens_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acct, "load_token", lambda: "tok")
    monkeypatch.setattr(
        acct, "api_post", lambda _p, _t, *a, **k: (200, {"url": "https://x/payout-setup?t=1"})
    )
    opened: list[str] = []
    monkeypatch.setattr(acct.webbrowser, "open", lambda u: opened.append(u) or True)
    assert acct.run(acct.payout_setup) == 0
    assert opened == ["https://x/payout-setup?t=1"]
