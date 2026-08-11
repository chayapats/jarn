"""Tests for secret storage, resolution, and keychain fallback."""

from __future__ import annotations

import os
import runpy
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jarn.config.secrets import (
    SecretResolutionError,
    SecretStorageError,
    is_reference,
    redact_secrets,
    resolve,
    store_secret,
)


def test_resolved_custom_secret_is_registered_for_all_sink_redaction(
    monkeypatch,
) -> None:
    import jarn.config.secrets as secrets

    credential = "arbitrary-provider-key-7q2"
    secrets._clear_resolved_secrets_for_testing()
    try:
        monkeypatch.setenv("CUSTOM_PROVIDER_KEY", credential)
        assert resolve("${CUSTOM_PROVIDER_KEY}") == credential
        assert redact_secrets(f"provider replied with {credential}") == (
            "provider replied with [REDACTED]"
        )

        # Local/test literals that are not credential-shaped must not become
        # global replacement strings merely because resolve() returned them.
        assert resolve("lm-studio") == "lm-studio"
        assert redact_secrets("use lm-studio locally") == "use lm-studio locally"
    finally:
        secrets._clear_resolved_secrets_for_testing()


def test_resolved_secret_registry_keeps_rotated_values_and_is_thread_safe(
    monkeypatch,
) -> None:
    import jarn.config.secrets as secrets

    old = "opaque-old-provider-credential"
    new = "opaque-new-provider-credential"
    secrets._clear_resolved_secrets_for_testing()
    try:
        monkeypatch.setenv("ROTATING_PROVIDER_KEY", old)
        assert resolve("${ROTATING_PROVIDER_KEY}") == old
        monkeypatch.setenv("ROTATING_PROVIDER_KEY", new)
        assert resolve("${ROTATING_PROVIDER_KEY}") == new

        values = [f"opaque-concurrent-credential-{index:03d}" for index in range(32)]
        for index, value in enumerate(values):
            monkeypatch.setenv(f"CONCURRENT_KEY_{index}", value)
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(
                pool.map(
                    lambda index: resolve(f"${{CONCURRENT_KEY_{index}}}"),
                    range(len(values)),
                )
            )
        assert resolved == values

        message = " ".join([old, new, *values])
        output = redact_secrets(message)
        assert all(value not in output for value in [old, new, *values])
    finally:
        secrets._clear_resolved_secrets_for_testing()


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


@pytest.mark.skipif(sys.platform == "win32", reason="Unix file permission bits")
def test_store_secret_timeout_fails_closed_without_file_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    def _hang(*_a, **_k):
        raise TimeoutError("no dbus")

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _hang)
    with pytest.raises(SecretStorageError, match="completion state is unknown"):
        store_secret("jarn", "openai_compatible", "sk-pi")
    secret_path = tmp_path / "home" / "secrets" / "jarn" / "openai_compatible"
    assert not secret_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX worker lifecycle contract")
@pytest.mark.parametrize(
    ("operation", "value"),
    [("set", "never-in-argv"), ("delete", None), ("metadata", None)],
)
def test_keyring_timeout_kills_and_reaps_worker_before_return(
    monkeypatch, tmp_path, operation, value
):
    """A timed-out keyring process cannot perform a late external mutation."""

    import jarn.config.secrets as secrets

    pid_path = tmp_path / "worker.pid"
    late_path = tmp_path / "late-write"
    worker = f"""
import json
import os
import pathlib
import sys
import time

json.load(sys.stdin)
pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))
time.sleep(5)
pathlib.Path({str(late_path)!r}).write_text("mutated")
"""
    monkeypatch.setattr(secrets, "_KEYRING_WORKER_CODE", worker)

    with pytest.raises(TimeoutError, match="did not respond"):
        secrets._keyring_call(operation, "jarn", "openrouter", value, timeout=0.5)

    pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    time.sleep(0.1)
    assert not late_path.exists()


def test_keyring_secret_is_sent_only_over_stdin(monkeypatch):
    import jarn.config.secrets as secrets

    credential = "raw-secret-never-in-process-arguments"
    observed: dict[str, object] = {}

    class FakeProcess:
        returncode = 0
        pid = 12345

        def __init__(self, args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs

        def communicate(self, input=None, timeout=None):
            observed["input"] = input
            observed["timeout"] = timeout
            return b'{"ok": true, "result": true}', b""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(secrets.subprocess, "Popen", FakeProcess)

    assert secrets._keyring_call("set", "jarn", "openrouter", credential, timeout=1.0)
    assert all(credential not in str(arg) for arg in observed["args"])
    assert credential.encode() in observed["input"]
    assert observed["kwargs"]["stderr"] is secrets.subprocess.PIPE


def test_keyring_backend_metadata_never_requests_a_credential(monkeypatch):
    import jarn.config.secrets as secrets

    request: dict[str, object] = {}

    def _metadata(op, service, account, value=None, *, timeout):
        request.update(
            op=op,
            service=service,
            account=account,
            value=value,
            timeout=timeout,
        )
        return {
            "available": True,
            "backend": "keyring.backends.test.Keyring",
            "priority": 1,
        }

    monkeypatch.setattr(secrets, "_keyring_call", _metadata)

    result = secrets.keyring_backend_metadata(timeout=0.25)

    assert request == {
        "op": "metadata",
        "service": "jarn",
        "account": "doctor",
        "value": None,
        "timeout": 0.25,
    }
    assert result["credentials_read"] is False


def test_frozen_keyring_worker_command_has_only_non_secret_selector(monkeypatch):
    import jarn.config.secrets as secrets

    monkeypatch.setattr(secrets.sys, "frozen", True, raising=False)
    monkeypatch.setattr(secrets.sys, "executable", "/opt/jarn/bin/jarn")
    assert secrets._keyring_worker_command() == [
        "/opt/jarn/bin/jarn",
        secrets._KEYRING_WORKER_SELECTOR,
    ]


def test_frozen_entry_dispatches_worker_without_cli_recursion(monkeypatch):
    import jarn.config.secrets as secrets

    calls: list[str] = []

    def _worker() -> int:
        calls.append("worker")
        return 37

    def _unexpected_cli() -> int:
        pytest.fail("internal keyring worker recursed into the user-facing CLI")

    monkeypatch.setattr(secrets, "_keyring_worker_main", _worker)
    monkeypatch.setattr("jarn.cli.main", _unexpected_cli)
    monkeypatch.setattr(sys, "argv", ["jarn", secrets._KEYRING_WORKER_SELECTOR])

    entry = Path(__file__).parents[1] / "packaging" / "entry.py"
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(entry), run_name="__main__")
    assert exit_info.value.code == 37
    assert calls == ["worker"]


def test_internal_keyring_worker_is_not_advertised_in_help():
    import jarn.config.secrets as secrets
    from jarn.cli import build_parser

    assert secrets._KEYRING_WORKER_SELECTOR not in build_parser().format_help()


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


def test_redact_short_known_secret_seven_chars():
    # A user-declared exact secret shorter than the heuristic floor (7 chars)
    # must still be scrubbed — it is a real credential, not detected text.
    out = redact_secrets("resolved credential abc1234 for header", known={"abc1234"})
    assert "abc1234" not in out
    assert "[REDACTED]" in out


def test_redact_short_known_secret_four_char_pin():
    # An even shorter realistic secret (a 4-char PIN) must not leak.
    out = redact_secrets("your PIN is 4821 ok", known={"4821"})
    assert "4821" not in out
    assert "[REDACTED]" in out


def test_redact_short_known_substring_keeps_longest_first():
    # A short known value that is a substring of a longer known value must not
    # corrupt the longer redaction: longest-first ordering still holds.
    short = "1234"
    long_ = "1234abcd"
    out = redact_secrets(f"key {long_} and pin {short}", known={short, long_})
    assert short not in out
    assert long_ not in out
    # If ordering were wrong the longer value would become "[REDACTED]abcd".
    assert "abcd" not in out


def test_redact_short_known_no_over_redaction():
    # Ordinary text without a matching known value is unchanged (no spurious
    # redaction from the exact-value loop or the pattern detectors).
    plain = "the meeting is at 4pm today"
    assert redact_secrets(plain, known={"4821"}) == plain


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


@pytest.mark.skipif(sys.platform == "win32", reason="Unix file permission bits")
def test_secret_tree_permissions(tmp_path, monkeypatch):
    """After file fallback, ~/.jarn/secrets/ and ancestors are 0700; file is 0600."""
    home = tmp_path / "home"
    monkeypatch.setenv("JARN_HOME", str(home))
    # Simulate a permissive pre-existing tree.
    secrets = home / "secrets" / "jarn"
    secrets.mkdir(parents=True)
    secrets.chmod(0o755)
    (home / "secrets").chmod(0o755)

    def _unavailable(*_a, **_k):
        raise RuntimeError("no keychain")

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _unavailable)
    store_secret("jarn", "openrouter", "sk-test")

    secret_file = home / "secrets" / "jarn" / "openrouter"
    assert secret_file.is_file()
    assert (secret_file.stat().st_mode & 0o777) == 0o600
    assert (secrets.stat().st_mode & 0o777) == 0o700
    assert ((home / "secrets").stat().st_mode & 0o777) == 0o700


def test_keychain_read_validates_account():
    with pytest.raises(SecretResolutionError, match="invalid secret account"):
        resolve("keychain:jarn/bad!")
