"""Tests : résolution de la clé API locale — fichier `ghost init` → env → erreur.

Le bug d'origine : `Anthropic()` sans clé ne lit QUE l'env, alors que
l'onboarding pose la clé dans ~/.ghost/api_key → « Could not resolve
authentication method » incompréhensible. Une seule source de vérité désormais.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

import ghost.onboard as onboard_mod
from ghost.onboard import ApiKeyMissing, resolve_api_key


def _keyfile(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "api_key"
    path.write_text(content, encoding="utf-8")
    return path


def test_resolve_file_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", _keyfile(tmp_path, "sk-ant-file\n"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert resolve_api_key() == "sk-ant-file"


def test_resolve_env_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", tmp_path / "absent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert resolve_api_key() == "sk-ant-env"


def test_resolve_empty_file_falls_back_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", _keyfile(tmp_path, "  \n"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert resolve_api_key() == "sk-ant-env"


def test_resolve_missing_raises_with_consigne(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", tmp_path / "absent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ApiKeyMissing, match="ghost init"):
        resolve_api_key()


# --------------------------------------------------------------------------
# Intégration : le client CLI reçoit la clé EXPLICITEMENT (api_key=…)


def test_cli_client_gets_file_key_without_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le scénario du bug : clé posée par `ghost init`, env vide → le client
    doit quand même être authentifié."""
    from ghost.cli import _anthropic_client

    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", _keyfile(tmp_path, "sk-ant-file\n"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _anthropic_client()
    assert client.api_key == "sk-ant-file"


def test_cli_client_exits_cleanly_without_any_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ghost.cli import _anthropic_client

    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", tmp_path / "absent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(typer.Exit):
        _anthropic_client()


def test_run_replay_clear_error_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plus de FileNotFoundError brut : ReplayError avec la consigne."""
    from ghost.replay import ReplayError, run_replay

    monkeypatch.setattr(onboard_mod, "API_KEY_FILE", tmp_path / "absent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ReplayError, match="ghost init"):
        run_replay(tmp_path, "prompt")
