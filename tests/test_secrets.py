"""Tests for secret storage, resolution, and keychain fallback."""

from __future__ import annotations

import pytest

from jarn.config.secrets import (
    SecretResolutionError,
    is_reference,
    resolve,
    store_secret,
)


def test_is_reference_includes_file():
    assert is_reference("file:jarn/openrouter")
    assert is_reference("keychain:jarn/openrouter")
    assert is_reference("${X}")
    assert not is_reference("literal")


def test_store_secret_uses_keychain_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    calls: list[tuple[str, str, str]] = []

    def _set(
        op: str,
        service: str,
        account: str,
        value: str | None = None,
        *,
        timeout: float,
    ) -> bool:
        assert op == "set"
        calls.append((service, account, value or ""))
        return True

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _set)
    stored = store_secret("jarn", "openrouter", "sk-test")
    assert stored.backend == "keychain"
    assert stored.reference == "keychain:jarn/openrouter"
    assert calls == [("jarn", "openrouter", "sk-test")]


def test_store_secret_falls_back_to_file_on_keychain_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    def _hang(*_a, **_k):
        raise TimeoutError("no dbus")

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _hang)
    stored = store_secret("jarn", "openai_compatible", "sk-pi")
    assert stored.backend == "file"
    assert stored.reference == "file:jarn/openai_compatible"
    assert resolve(stored.reference) == "sk-pi"
    secret_path = tmp_path / "home" / "secrets" / "jarn" / "openai_compatible"
    assert secret_path.is_file()
    assert oct(secret_path.stat().st_mode & 0o777) == oct(0o600)


def test_store_secret_falls_back_to_file_on_keyring_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    def _fail(*_a, **_k):
        raise RuntimeError("Secret Service unavailable")

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _fail)
    stored = store_secret("jarn", "openai", "sk-x")
    assert stored.backend == "file"
    assert resolve(stored.reference) == "sk-x"


def test_resolve_file_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    with pytest.raises(SecretResolutionError, match="No file secret"):
        resolve("file:jarn/missing")


def test_file_fallback_notice_lists_next_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.config.secrets import StoredSecret, file_fallback_notice

    stored = StoredSecret(reference="file:jarn/openai_compatible", backend="file")
    text = file_fallback_notice(
        stored,
        provider="openai_compatible",
        env_var="OPENAI_COMPATIBLE_API_KEY",
    )
    assert text is not None
    assert "What to do next" in text
    assert "Continue setup" in text
    assert "Launch with `jarn`" in text
    assert "OPENAI_COMPATIBLE_API_KEY" in text
    assert "gnome-keyring" in text


def test_file_fallback_notice_none_for_keychain():
    from jarn.config.secrets import StoredSecret, file_fallback_notice

    stored = StoredSecret(reference="keychain:jarn/openrouter", backend="keychain")
    assert file_fallback_notice(stored, provider="openrouter") is None


def test_resolve_keychain_timeout_raises_clear_message(monkeypatch):
    def _hang(*_a, **_k):
        raise TimeoutError("no dbus")

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _hang)
    with pytest.raises(SecretResolutionError, match="Timed out reading keychain"):
        resolve("keychain:jarn/openrouter")
