"""Tests for secret storage, resolution, and keychain fallback."""

from __future__ import annotations

import pytest

from jarn.config.secrets import (
    SecretResolutionError,
    is_reference,
    redact_secrets,
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


# ── Central secret redaction ────────────────────────────────────────────────


def test_redact_sk_key_keeps_prefix_and_last4():
    out = redact_secrets("the key is sk-proj-ABCDEFGH1234567890WXYZ ends")
    assert out.startswith("the key is sk-…")
    assert out.endswith("WXYZ ends")
    assert "ABCDEFGH1234567890" not in out


def test_redact_bearer_token():
    out = redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "Bearer [REDACTED]" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_redact_pem_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA0Z3VSXb...lots...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert redact_secrets(pem) == "[REDACTED]"


def test_redact_vendor_keys():
    out = redact_secrets("ghp_1234567890abcdefghij xoxb-1234567890-abc AKIAIOSFODNN7EXAMPLE")
    assert "ghp_1234567890" not in out
    assert "xoxb-1234567890-abc" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_redact_name_value_sensitive_var():
    out = redact_secrets("DATABASE_PASSWORD=hunter2 connect")
    assert "DATABASE_PASSWORD=[REDACTED]" in out
    assert "hunter2" not in out


def test_redact_name_value_non_sensitive_left_alone():
    plain = "FOO=bar baz=qux"
    assert redact_secrets(plain) == plain


def test_redact_base64_blob_with_two_distinct_chars():
    blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij1234567890"  # 46 varied chars
    out = redact_secrets(f"token={blob}")
    assert blob not in out
    assert "[REDACTED]" in out


def test_redact_preserves_single_char_padding():
    # Truncation tests in test_transcript use long single-char runs; the
    # base64 heuristic must not wipe those (they are not secrets).
    padding = "x" * 200
    assert redact_secrets(padding) == padding


def test_redact_known_secret_verbatim():
    out = redact_secrets("auth raw-9f8a7b6c5d4e3f2a1b0c no-prefix-here", known={"raw-9f8a7b6c5d4e3f2a1b0c"})
    assert "raw-9f8a7b6c5d4e3f2a1b0c" not in out
    assert "[REDACTED]" in out


def test_redact_known_secret_substring_of_longer_first():
    short = "abcdefgh"
    long_ = "abcdefghijklmnop"
    out = redact_secrets(f"{long_} and {short}", known={short, long_})
    assert short not in out
    assert long_ not in out


def test_redact_empty_returns_empty():
    assert redact_secrets("") == ""


def test_redact_keychain_error_scrubs_exc(monkeypatch):
    # A backend error that interpolates a secret value must be redacted.
    leaked = "sk-proj-LEAKEDKEY1234567890ABCD"

    def _fail(*_a, **_k):
        raise RuntimeError(f"backend said: {leaked}")

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _fail)
    with pytest.raises(SecretResolutionError) as ei:
        resolve("keychain:jarn/openrouter")
    assert leaked not in str(ei.value)
    assert "sk-…" in str(ei.value)


def test_secret_tree_permissions(tmp_path, monkeypatch):
    """After file fallback, ~/.jarn/secrets/ and ancestors are 0700; file is 0600."""
    home = tmp_path / "home"
    monkeypatch.setenv("JARN_HOME", str(home))
    # Simulate a permissive pre-existing tree.
    secrets = home / "secrets" / "jarn"
    secrets.mkdir(parents=True)
    secrets.chmod(0o755)
    (home / "secrets").chmod(0o755)

    def _timeout(*_a, **_k):
        raise TimeoutError("no keychain")

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _timeout)
    store_secret("jarn", "openrouter", "sk-test")

    secret_file = home / "secrets" / "jarn" / "openrouter"
    assert secret_file.is_file()
    assert (secret_file.stat().st_mode & 0o777) == 0o600
    assert (secrets.stat().st_mode & 0o777) == 0o700
    assert ((home / "secrets").stat().st_mode & 0o777) == 0o700


def test_keychain_read_validates_account():
    with pytest.raises(SecretResolutionError, match="invalid secret account"):
        resolve("keychain:jarn/bad!")
