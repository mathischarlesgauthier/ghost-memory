"""Client réseau Ghost : device login + upgrade. Le jeton Ghost identifie
l'utilisateur pour le réseau (JAMAIS la clé Anthropic). Stocké en 0600.

Transport injectable (`Http`) pour tester sans serveur ni réseau.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

DEFAULT_API_BASE = "https://ghost-backend-production-f062.up.railway.app"
TOKEN_FILE = Path.home() / ".ghost" / "ghost_token"

# (method, url, json_body|None, token|None) -> (status_code, parsed_json)
Http = Callable[[str, str, dict[str, object] | None, str | None], tuple[int, dict[str, object]]]


class NetworkError(RuntimeError):
    pass


def api_base() -> str:
    return os.environ.get("GHOST_API_URL", DEFAULT_API_BASE).rstrip("/")


def _urllib_http(
    method: str, url: str, body: dict[str, object] | None, token: str | None
) -> tuple[int, dict[str, object]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"detail": raw[:200]}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise NetworkError(f"réseau injoignable ({exc.reason})") from exc


def save_token(token: str, path: Path = TOKEN_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(token.strip() + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def load_token(path: Path = TOKEN_FILE) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def clear_token(path: Path = TOKEN_FILE) -> None:
    path.unlink(missing_ok=True)


def api_get(
    path: str, token: str, *, base: str | None = None, http: Http = _urllib_http
) -> tuple[int, dict[str, object]]:
    return http("GET", (base or api_base()) + path, None, token)


def api_post(
    path: str,
    token: str,
    body: dict[str, object] | None = None,
    *,
    base: str | None = None,
    http: Http = _urllib_http,
) -> tuple[int, dict[str, object]]:
    return http("POST", (base or api_base()) + path, body, token)


def _cache_file(name: str) -> Path:
    return TOKEN_FILE.parent / f"cache_{name}.json"


def read_cache(name: str) -> dict[str, object] | None:
    """Dernier état connu (pour l'affichage dégradé hors ligne)."""
    path = _cache_file(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"_list": data}
    except (OSError, json.JSONDecodeError):
        return None


def write_cache(name: str, data: object) -> None:
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _cache_file(name).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def device_login(
    *,
    base: str | None = None,
    http: Http = _urllib_http,
    on_prompt: Callable[[str, str], None] = lambda uri, code: None,
    poll: Callable[[int], None] = lambda _s: None,
    max_polls: int = 180,
) -> str:
    """Device flow complet → renvoie le jeton Ghost (non persisté ici)."""
    base = base or api_base()
    status, code = http("POST", f"{base}/auth/device/code", {}, None)
    if status != 200:
        raise NetworkError(f"impossible d'initier le login ({status}): {code.get('detail')}")
    device_code = str(code["device_code"])
    on_prompt(str(code["verification_uri"]), str(code["user_code"]))
    raw_interval = code.get("interval", 5)
    interval = raw_interval if isinstance(raw_interval, int) else 5
    for _ in range(max_polls):
        status, resp = http(
            "POST", f"{base}/auth/device/token", {"device_code": device_code}, None
        )
        if status == 200:
            return str(resp["access_token"])
        if status == 428:  # authorization_pending
            poll(interval)
            continue
        raise NetworkError(f"login échoué ({status}): {resp.get('detail')}")
    raise NetworkError("délai de login dépassé — relance `ghost login`")


def checkout_url(
    tier: str, token: str, *, base: str | None = None, http: Http = _urllib_http
) -> str:
    base = base or api_base()
    status, resp = http("POST", f"{base}/billing/checkout?tier={tier}", None, token)
    if status != 200:
        raise NetworkError(f"checkout impossible ({status}): {resp.get('detail')}")
    url = resp.get("checkout_url")
    if not url:
        raise NetworkError(str(resp.get("note") or "Stripe non configuré côté serveur"))
    return str(url)
