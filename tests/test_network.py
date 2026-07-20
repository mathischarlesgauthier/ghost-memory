"""Lot G (côté CLI) : store de jeton + device login + checkout, transport mocké."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghost.network import (
    NetworkError,
    checkout_url,
    clear_token,
    device_login,
    load_token,
    retrieve,
    save_token,
)


def test_token_store_roundtrip_chmod(tmp_path: Path) -> None:
    p = tmp_path / "ghost_token"
    save_token("ghost_abc  ", path=p)
    assert load_token(p) == "ghost_abc"
    assert (p.stat().st_mode & 0o777) == 0o600
    clear_token(p)
    assert load_token(p) is None


def test_device_login_polls_until_verified() -> None:
    calls = {"token": 0}
    prompted: list[tuple[str, str]] = []

    def http(method: str, url: str, body: object, token: object) -> tuple[int, dict]:
        if url.endswith("/auth/device/code"):
            return 200, {
                "device_code": "dc", "user_code": "K7Q2-9FMX",
                "verification_uri": "https://ghost-memory.com/device", "interval": 0,
            }
        if url.endswith("/auth/device/token"):
            calls["token"] += 1
            if calls["token"] < 3:
                return 428, {"detail": "authorization_pending"}
            return 200, {"access_token": "ghost_live_token"}
        raise AssertionError(url)

    token = device_login(
        base="https://api",
        http=http,  # type: ignore[arg-type]
        on_prompt=lambda uri, code: prompted.append((uri, code)),
        poll=lambda _s: None,
    )
    assert token == "ghost_live_token"
    assert calls["token"] == 3  # a bien attendu la vérification
    assert prompted and prompted[0][1] == "K7Q2-9FMX"


def test_device_login_raises_on_error() -> None:
    def http(method: str, url: str, body: object, token: object) -> tuple[int, dict]:
        if url.endswith("/code"):
            return 200, {"device_code": "dc", "user_code": "X", "verification_uri": "u"}
        return 500, {"detail": "boom"}

    with pytest.raises(NetworkError, match="login échoué"):
        device_login(base="https://api", http=http, poll=lambda _s: None)  # type: ignore[arg-type]


def test_checkout_url_returns_link() -> None:
    def http(method: str, url: str, body: object, token: object) -> tuple[int, dict]:
        assert token == "ghost_tok" and "tier=pro" in url
        return 200, {"checkout_url": "https://checkout.stripe.com/x"}

    assert checkout_url("pro", "ghost_tok", base="https://api", http=http) == (  # type: ignore[arg-type]
        "https://checkout.stripe.com/x"
    )


def test_checkout_url_raises_when_unconfigured() -> None:
    def http(method: str, url: str, body: object, token: object) -> tuple[int, dict]:
        return 200, {"checkout_url": None, "note": "Stripe non configuré"}

    with pytest.raises(NetworkError, match="Stripe non configuré"):
        checkout_url("pro", "t", base="https://api", http=http)  # type: ignore[arg-type]


def test_retrieve_builds_query_and_parses() -> None:
    seen: dict[str, object] = {}

    def http(method: str, url: str, body: object, token: object) -> tuple[int, dict]:
        seen.update(method=method, url=url, token=token)
        return 200, {
            "skills": [{"slug": "x", "mean_lift": None, "status": "unverified", "seed": True}],
            "message": None,
        }

    resp = retrieve("bash|py|err|commit", "tok", limit=5, base="https://api.test", http=http)
    assert seen["method"] == "GET"
    assert "/registry/retrieve?" in str(seen["url"])
    assert "signature=bash" in str(seen["url"]) and "limit=5" in str(seen["url"])
    assert seen["token"] == "tok"
    assert resp["skills"][0]["slug"] == "x"  # type: ignore[index]


def test_retrieve_401_raises() -> None:
    def http(method: str, url: str, body: object, token: object) -> tuple[int, dict]:
        return 401, {"detail": "nope"}

    with pytest.raises(NetworkError):
        retrieve("s", "tok", base="https://api.test", http=http)
