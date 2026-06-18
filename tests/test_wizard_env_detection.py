"""Tests for P2.A — beginner-friendly wizard env detection and recommendation logic.

Covers:
- _detect_env_key: returns (provider, env_var) when a key is set, None otherwise.
- _recommended_provider: correct recommendation in all three scenarios.
- _provider_hint: correct cloud/local/custom labels.
- _configure_key: stores ${ENV} reference (never the verbatim key) when env_hit matches.
"""

from __future__ import annotations

from jarn.onboarding.wizard import (
    _configure_key,
    _detect_env_key,
    _provider_hint,
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

    def test_anthropic_recommended_when_key_present_but_no_env_hit(self, monkeypatch):
        """env_hit is None but ANTHROPIC_API_KEY is set → anthropic recommended."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        result = _recommended_provider(None)
        assert result == "anthropic"

    def test_openrouter_default_when_nothing_present(self, monkeypatch):
        """No env hits at all → openrouter (current default)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = _recommended_provider(None)
        assert result == "openrouter"


# ---------------------------------------------------------------------------
# _provider_hint
# ---------------------------------------------------------------------------

class TestProviderHint:
    def test_cloud_providers(self):
        from jarn.config.defaults import CLOUD_PROVIDERS, CUSTOM_OPENAI_PROFILE
        for p in CLOUD_PROVIDERS:
            if p == CUSTOM_OPENAI_PROFILE:
                continue
            assert _provider_hint(p) == "cloud", f"{p} should be 'cloud'"

    def test_custom_provider(self):
        from jarn.config.defaults import CUSTOM_OPENAI_PROFILE
        assert _provider_hint(CUSTOM_OPENAI_PROFILE) == "custom"

    def test_local_providers(self):
        for p in ("ollama", "lmstudio"):
            assert _provider_hint(p) == "local", f"{p} should be 'local'"


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


def test_ping_with_timeout_returns_fast_response():
    """A model that responds in time returns its response normally."""
    from jarn.onboarding.wizard import _ping_with_timeout

    class _FastChat:
        def invoke(self, _prompt):
            return "pong"

    assert _ping_with_timeout(_FastChat(), timeout=5.0) == "pong"


def test_ping_with_timeout_raises_on_slow_model():
    """A model slower than the timeout raises TimeoutError instead of hanging setup
    forever (regression: validation blocked silently on a cold-loading model)."""
    import time as _time

    import pytest

    from jarn.onboarding.wizard import _ping_with_timeout

    class _SlowChat:
        def invoke(self, _prompt):
            _time.sleep(5)  # well beyond the timeout
            return "late"

    with pytest.raises(TimeoutError):
        _ping_with_timeout(_SlowChat(), timeout=0.2)


def test_ping_with_timeout_propagates_invoke_error():
    """An error from the model surfaces to the caller (not swallowed by the thread)."""
    import pytest

    from jarn.onboarding.wizard import _ping_with_timeout

    class _BadChat:
        def invoke(self, _prompt):
            raise ValueError("bad key")

    with pytest.raises(ValueError, match="bad key"):
        _ping_with_timeout(_BadChat(), timeout=5.0)
