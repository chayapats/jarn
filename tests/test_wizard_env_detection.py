"""Tests for P2.A — beginner-friendly wizard env detection and recommendation logic.

Covers:
- _detect_env_key: returns (provider, env_var) when a key is set, None otherwise.
- _recommended_provider: correct recommendation in all three scenarios.
- provider_hint: correct cloud/local/custom labels.
- _configure_key: stores ${ENV} reference (never the verbatim key) when env_hit matches.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarn.onboarding.providers import provider_hint
from jarn.onboarding.wizard import (
    _configure_key,
    _detect_env_key,
    _recommended_provider,
)

# ---------------------------------------------------------------------------
# _detect_env_key
# ---------------------------------------------------------------------------

class TestDetectEnvKey:
    def test_returns_none_when_no_keys_set(self, monkeypatch):
        """No env vars set → None."""
        from jarn.config.defaults import PROVIDER_ENV_VARS
        for ev in PROVIDER_ENV_VARS.values():
            monkeypatch.delenv(ev, raising=False)
        assert _detect_env_key() is None

    def test_detects_opencode_key(self, monkeypatch):
        from jarn.config.defaults import PROVIDER_ENV_VARS
        for ev in PROVIDER_ENV_VARS.values():
            monkeypatch.delenv(ev, raising=False)
        monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode-test")
        result = _detect_env_key()
        assert result == ("opencode", "OPENCODE_API_KEY")

    def test_detects_anthropic_key(self, monkeypatch):
        from jarn.config.defaults import PROVIDER_ENV_VARS
        for ev in PROVIDER_ENV_VARS.values():
            monkeypatch.delenv(ev, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        result = _detect_env_key()
        assert result == ("anthropic", "ANTHROPIC_API_KEY")

    def test_detects_openai_key(self, monkeypatch):
        from jarn.config.defaults import PROVIDER_ENV_VARS
        for ev in PROVIDER_ENV_VARS.values():
            monkeypatch.delenv(ev, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        result = _detect_env_key()
        assert result == ("openai", "OPENAI_API_KEY")

    def test_detects_openrouter_key(self, monkeypatch):
        from jarn.config.defaults import PROVIDER_ENV_VARS
        for ev in PROVIDER_ENV_VARS.values():
            monkeypatch.delenv(ev, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        result = _detect_env_key()
        assert result == ("openrouter", "OPENROUTER_API_KEY")

    def test_opencode_takes_priority_over_anthropic(self, monkeypatch):
        """opencode is checked before anthropic in priority order."""
        from jarn.config.defaults import PROVIDER_ENV_VARS
        for ev in PROVIDER_ENV_VARS.values():
            monkeypatch.delenv(ev, raising=False)
        monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode-test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        result = _detect_env_key()
        assert result is not None
        assert result[0] == "opencode"

    def test_anthropic_takes_priority_over_openrouter(self, monkeypatch):
        """anthropic is checked before openrouter in priority order."""
        from jarn.config.defaults import PROVIDER_ENV_VARS
        for ev in PROVIDER_ENV_VARS.values():
            monkeypatch.delenv(ev, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        result = _detect_env_key()
        assert result is not None
        assert result[0] == "anthropic"

    def test_detects_other_provider_key(self, monkeypatch):
        """A non-priority provider key is still detected."""
        from jarn.config.defaults import PROVIDER_ENV_VARS
        for ev in PROVIDER_ENV_VARS.values():
            monkeypatch.delenv(ev, raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
        result = _detect_env_key()
        assert result is not None
        assert result[0] == "groq"
        assert result[1] == "GROQ_API_KEY"


# ---------------------------------------------------------------------------
# _recommended_provider
# ---------------------------------------------------------------------------

class TestRecommendedProvider:
    def test_env_hit_provider_is_recommended(self, monkeypatch):
        """When an env key is found, that provider is recommended."""
        result = _recommended_provider(("anthropic", "ANTHROPIC_API_KEY"))
        assert result == "anthropic"

    def test_openrouter_env_hit_is_recommended(self, monkeypatch):
        result = _recommended_provider(("openrouter", "OPENROUTER_API_KEY"))
        assert result == "openrouter"

    def test_opencode_recommended_when_key_present_but_no_env_hit(self, monkeypatch):
        """env_hit is None but OPENCODE_API_KEY is set → opencode recommended."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode-test")
        result = _recommended_provider(None)
        assert result == "opencode"

    def test_anthropic_recommended_when_key_present_but_no_env_hit(self, monkeypatch):
        """env_hit is None but ANTHROPIC_API_KEY is set → anthropic recommended."""
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        result = _recommended_provider(None)
        assert result == "anthropic"

    def test_chatgpt_default_when_nothing_present(self, monkeypatch):
        """No env hits at all → the simple ChatGPT subscription path."""
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = _recommended_provider(None)
        assert result == "codex_subscription"


# ---------------------------------------------------------------------------
# provider_hint
# ---------------------------------------------------------------------------

class TestProviderHint:
    def test_cloud_providers(self):
        from jarn.config.defaults import CLOUD_PROVIDERS, CUSTOM_OPENAI_PROFILE
        for p in CLOUD_PROVIDERS:
            if p == CUSTOM_OPENAI_PROFILE:
                continue
            assert provider_hint(p) == "cloud", f"{p} should be 'cloud'"

    def test_custom_provider(self):
        from jarn.config.defaults import CUSTOM_OPENAI_PROFILE
        assert provider_hint(CUSTOM_OPENAI_PROFILE) == "custom"

    def test_local_providers(self):
        for p in ("ollama", "lmstudio"):
            assert provider_hint(p) == "local", f"{p} should be 'local'"


# ---------------------------------------------------------------------------
# _configure_key — env_hit path (does NOT store verbatim key)
# ---------------------------------------------------------------------------

class TestConfigureKeyEnvHit:
    def test_returns_env_ref_not_verbatim_key_when_env_hit_matches(self, monkeypatch):
        """When env_hit matches the provider, _configure_key must return the
        ${ENV_VAR} reference form — never the actual key value."""
        # Simulate ANTHROPIC_API_KEY present in env
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-actual-secret")
        result = _configure_key("anthropic", env_hit=("anthropic", "ANTHROPIC_API_KEY"))
        # Must be the reference form, not the verbatim key
        assert result == "${ANTHROPIC_API_KEY}"
        assert "sk-ant-actual-secret" not in (result or "")

    def test_returns_env_ref_for_openrouter(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-actual")
        result = _configure_key("openrouter", env_hit=("openrouter", "OPENROUTER_API_KEY"))
        assert result == "${OPENROUTER_API_KEY}"
        assert "sk-or-actual" not in (result or "")

    def test_no_env_hit_does_not_skip_prompt(self, monkeypatch):
        """When env_hit is None the function should still ask (covered by prompting path).
        We test that passing env_hit for a *different* provider does not bypass the prompt."""
        # env_hit refers to anthropic but we ask for openai — should NOT auto-return
        # We can't test the interactive prompt path directly without mocking, so we verify
        # that passing a mismatched env_hit does NOT return a value directly.
        # The simplest check: _configure_key with env_hit for a different provider
        # won't hit the early-return branch.
        # We mock Prompt.ask to return "env" so the function completes.
        from unittest.mock import patch
        with patch("jarn.onboarding.wizard.Prompt.ask", return_value="env"):
            result = _configure_key(
                "openai",
                env_hit=("anthropic", "ANTHROPIC_API_KEY"),  # mismatch
            )
        assert result == "${OPENAI_API_KEY}"

    def test_local_provider_returns_none_regardless_of_env_hit(self):
        """Local providers never need a key."""
        result = _configure_key("ollama", env_hit=None)
        assert result is None
        result2 = _configure_key("lmstudio", env_hit=None)
        assert result2 is None


# -- Fix B: validation ping timeout (don't hang setup on a cold model) -------


@pytest.mark.skipif(os.name != "posix", reason="supported validation runtime is POSIX")
def test_ping_with_timeout_returns_fast_response():
    """A model that responds in time returns its response normally."""
    from jarn.onboarding.wizard import _ping_with_timeout

    class _FastChat:
        def invoke(self, _prompt):
            return "pong"

    assert _ping_with_timeout(_FastChat(), timeout=5.0) == "pong"


@pytest.mark.skipif(os.name != "posix", reason="supported validation runtime is POSIX")
def test_ping_with_timeout_raises_on_slow_model(tmp_path: Path):
    """A model slower than the timeout raises TimeoutError instead of hanging setup
    forever (regression: validation blocked silently on a cold-loading model)."""
    from jarn.onboarding.wizard import _ping_with_timeout

    pid_path = tmp_path / "direct-worker.pid"
    late_path = tmp_path / "late-direct-side-effect"

    class _SlowChat:
        def invoke(self, _prompt):
            pid_path.write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(5)  # well beyond the timeout
            late_path.write_text("too late", encoding="utf-8")
            return "late"

    with pytest.raises(TimeoutError):
        _ping_with_timeout(_SlowChat(), timeout=0.3)
    # On a contended runner the parent may reach the deadline before the forked
    # child gets its first timeslice.  That is a valid pre-handshake timeout:
    # the direct child is killed and reaped before ``invoke`` can run, so there
    # is no PID file to inspect.  If invocation did start, its recorded PID
    # must already be gone.
    if pid_path.exists():
        pid = int(pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    time.sleep(0.1)
    assert not late_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="supported validation runtime is POSIX")
def test_ping_timeout_before_process_group_ready_kills_direct_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A scheduler delay before setsid cannot race timeout cleanup.

    The child advertises readiness only after creating its isolated process
    group.  Before that handshake, direct-child SIGKILL is safe because model
    invocation (and therefore descendant creation) has not begun.
    """
    from jarn.onboarding import wizard

    child_pid_path = tmp_path / "pre-handshake.pid"
    invoked_path = tmp_path / "model-was-invoked"
    real_setsid = os.setsid

    def _delayed_setsid():
        child_pid_path.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(1.0)
        return real_setsid()

    monkeypatch.setattr(wizard.os, "setsid", _delayed_setsid)

    class _MustNotRun:
        def invoke(self, _prompt):
            invoked_path.write_text("unsafe", encoding="utf-8")
            return "late"

    with pytest.raises(TimeoutError):
        wizard._ping_with_timeout(_MustNotRun(), timeout=0.1)

    # A heavily loaded runner can expire the 100 ms deadline before the child
    # enters the patched ``setsid`` at all.  Both schedules exercise the same
    # contract: either the started child records a PID and is gone, or it never
    # reaches model invocation before being synchronously reaped.
    if child_pid_path.exists():
        pid = int(child_pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not invoked_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="supported validation runtime is POSIX")
def test_ping_with_timeout_propagates_invoke_error():
    """An error from the model surfaces to the caller (not swallowed by the thread)."""
    import pytest

    from jarn.onboarding.wizard import _ping_with_timeout

    class _BadChat:
        def invoke(self, _prompt):
            raise ValueError("bad key")

    with pytest.raises(ValueError, match="bad key"):
        _ping_with_timeout(_BadChat(), timeout=5.0)


@pytest.mark.skipif(os.name != "posix", reason="supported validation runtime is POSIX")
def test_validation_timeout_kills_reaps_process_group_and_prevents_late_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarn.onboarding.wizard as wizard

    pid_path = tmp_path / "validation-worker.pid"
    late_path = tmp_path / "late-validation-side-effect"
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
    monkeypatch.setattr(wizard, "_VALIDATION_WORKER_CODE", worker)

    with pytest.raises(TimeoutError, match="no response within"):
        wizard._run_validation_request(
            "ollama",
            "ollama/qwen3",
            {"providers": {"ollama": {"type": "ollama"}}},
            timeout=0.5,
        )

    pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    time.sleep(0.1)
    assert not late_path.exists()


def test_validation_credential_and_config_travel_only_over_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarn.onboarding.wizard as wizard

    secret = "raw-validation-secret-never-in-argv"
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
            return b'{"ok":true,"response_chars":4}', b""

        def poll(self):
            return self.returncode

    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setattr(wizard.subprocess, "Popen", FakeProcess)
    config = {
        "providers": {
            "anthropic": {"type": "anthropic", "api_key": "${ANTHROPIC_API_KEY}"}
        }
    }

    assert (
        wizard._run_validation_request(
            "anthropic",
            "anthropic/claude-test",
            config,
            timeout=3.0,
        )
        == 4
    )
    assert all(secret not in str(argument) for argument in observed["args"])
    request = json.loads(observed["input"])
    assert request["credential"] == secret
    assert request["config"]["providers"]["anthropic"]["api_key"] is None
    assert observed["kwargs"]["stderr"] is subprocess.PIPE
    assert "ANTHROPIC_API_KEY" not in observed["kwargs"]["env"]
    assert config["providers"]["anthropic"]["api_key"] == "${ANTHROPIC_API_KEY}"


@pytest.mark.parametrize(
    "worker_output",
    [
        b"not-json",
        b"[]",
        b'{"ok":true,"response_chars":"four"}',
    ],
)
def test_validation_worker_malformed_responses_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    worker_output: bytes,
) -> None:
    import jarn.onboarding.wizard as wizard

    class FakeProcess:
        returncode = 0
        pid = 12345

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            return worker_output, b"secret-looking worker stderr is ignored"

        def poll(self):
            return self.returncode

    monkeypatch.setattr(wizard.subprocess, "Popen", FakeProcess)

    with pytest.raises(wizard.ValidationWorkerError, match="invalid"):
        wizard._run_validation_request(
            "ollama",
            "ollama/qwen3",
            {"providers": {"ollama": {"type": "ollama"}}},
            timeout=1.0,
        )


def test_validation_worker_error_is_bounded_and_secret_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarn.onboarding.wizard as wizard

    secret = "arbitrary-short-secret"
    response = json.dumps(
        {
            "ok": False,
            "error_type": "ProviderError",
            "message": f"provider echoed {secret}",
        }
    ).encode()

    class FakeProcess:
        returncode = 0
        pid = 12345

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            return response, b""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(wizard.subprocess, "Popen", FakeProcess)

    with pytest.raises(wizard.ValidationWorkerError) as error:
        wizard._run_validation_request(
            "anthropic",
            "anthropic/claude-test",
            {"providers": {"anthropic": {"type": "anthropic", "api_key": secret}}},
            timeout=1.0,
        )
    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_frozen_validation_worker_command_has_only_private_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarn.onboarding.wizard as wizard

    monkeypatch.setattr(wizard.sys, "frozen", True, raising=False)
    monkeypatch.setattr(wizard.sys, "executable", "/opt/jarn/bin/jarn")

    assert wizard._validation_worker_command() == [
        "/opt/jarn/bin/jarn",
        wizard._VALIDATION_WORKER_SELECTOR,
    ]


def test_frozen_entry_dispatches_validation_worker_without_cli_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarn.onboarding.wizard as wizard

    calls: list[str] = []

    def worker() -> int:
        calls.append("validation")
        return 41

    def unexpected_cli() -> int:
        pytest.fail("internal validation worker recursed into the public CLI")

    monkeypatch.setattr(wizard, "_validation_worker_main", worker)
    monkeypatch.setattr("jarn.cli.main", unexpected_cli)
    monkeypatch.setattr(sys, "argv", ["jarn", wizard._VALIDATION_WORKER_SELECTOR])

    entry = Path(__file__).parents[1] / "packaging" / "entry.py"
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(entry), run_name="__main__")
    assert exit_info.value.code == 41
    assert calls == ["validation"]


def test_internal_validation_worker_is_not_advertised_in_help() -> None:
    from jarn.cli import build_parser
    from jarn.onboarding.wizard import _VALIDATION_WORKER_SELECTOR

    assert _VALIDATION_WORKER_SELECTOR not in build_parser().format_help()


def test_source_validation_worker_constructs_model_and_returns_bounded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarn.onboarding.wizard as wizard

    monkeypatch.setenv("JARN_DEMO", "1")
    response_chars = wizard._run_validation_request(
        "openrouter",
        "openrouter/demo",
        {
            "default_profile": "openrouter",
            "default_model": "openrouter/demo",
            "providers": {"openrouter": {"type": "openrouter", "api_key": None}},
        },
        timeout=10.0,
    )

    assert 0 < response_chars < 10_000


def test_validation_worker_environment_disables_remote_tracing_and_scrubs_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarn.onboarding.wizard as wizard

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "opaque-tracing-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=opaque")
    monkeypatch.setenv("JARN_SETUP_CREDENTIAL_TEST", "opaque-provider-secret")

    env = wizard._validation_worker_env("JARN_SETUP_CREDENTIAL_TEST")

    assert env["LANGSMITH_TRACING"] == "false"
    assert env["LANGCHAIN_TRACING_V2"] == "false"
    assert "LANGSMITH_API_KEY" not in env
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    assert "JARN_SETUP_CREDENTIAL_TEST" not in env


def test_validate_config_preserves_success_and_timeout_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    from rich.console import Console

    import jarn.onboarding.wizard as wizard

    stream = StringIO()
    monkeypatch.setattr(wizard, "console", Console(file=stream, force_terminal=False))
    monkeypatch.setattr(wizard, "_run_validation_request", lambda *_args, **_kwargs: 7)

    assert wizard.validate_config("openrouter", "openrouter/demo", {}) is True
    assert "model responded (7 chars)" in stream.getvalue()

    def timeout(*_args, **_kwargs):
        raise TimeoutError("no response")

    stream.seek(0)
    stream.truncate(0)
    monkeypatch.setattr(wizard, "_run_validation_request", timeout)
    assert wizard.validate_config("openrouter", "openrouter/demo", {}, timeout=2.0) is False
    output = stream.getvalue()
    assert "validation timed out after 2s" in output
    assert "isolated request was stopped" in output
