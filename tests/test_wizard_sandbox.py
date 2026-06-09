"""Tests for the wizard's OS-sandbox recommendation in the generated config.

Verifies that _build_config_dict emits an 'execution' block with
local_sandbox='auto', consistent with M1.2: making the lightweight OS sandbox
the recommended isolation path for untrusted repos.
"""

from __future__ import annotations

from jarn.onboarding.wizard import _build_config_dict


class TestWizardExecutionBlock:
    """_build_config_dict must include an 'execution' key with local_sandbox: 'auto'."""

    def _make_config(self, **kwargs):
        """Return a config dict using minimal but valid wizard inputs."""
        return _build_config_dict(
            "openrouter",
            "${OPENROUTER_API_KEY}",
            "openrouter/anthropic/claude-opus-4-8",
            "dark",
            **kwargs,
        )

    def test_execution_key_present(self):
        """The returned dict must have an 'execution' top-level key."""
        cfg = self._make_config()
        assert "execution" in cfg, "'execution' key missing from wizard config"

    def test_local_sandbox_is_auto(self):
        """execution.local_sandbox must be 'auto' — use kernel sandbox when
        available, degrade-with-warning when not (never blocks startup)."""
        cfg = self._make_config()
        assert cfg["execution"]["local_sandbox"] == "auto"

    def test_execution_block_is_dict(self):
        """The 'execution' value must be a dict (not a string or None)."""
        cfg = self._make_config()
        assert isinstance(cfg["execution"], dict)

    def test_execution_block_present_for_local_provider(self):
        """Recommendation applies regardless of provider — local providers
        (e.g. ollama) also benefit from the OS sandbox."""
        cfg = _build_config_dict(
            "ollama",
            None,  # no API key for local provider
            "ollama/llama3",
            "dark",
        )
        assert "execution" in cfg
        assert cfg["execution"]["local_sandbox"] == "auto"

    def test_execution_block_present_with_custom_mode(self):
        """Execution block must appear even when a non-default permission mode
        is passed to _build_config_dict."""
        cfg = _build_config_dict(
            "openrouter",
            "${OPENROUTER_API_KEY}",
            "openrouter/anthropic/claude-opus-4-8",
            "dark",
            mode="yolo",
        )
        assert cfg["execution"]["local_sandbox"] == "auto"

    def test_other_top_level_keys_unaffected(self):
        """Adding the execution block must not remove or alter any existing
        top-level keys that were present before M1.2."""
        cfg = self._make_config()
        for key in ("default_profile", "default_model", "permission_mode",
                    "providers", "routing", "budget", "context",
                    "permissions", "observability", "ui"):
            assert key in cfg, f"expected top-level key {key!r} missing after M1.2 change"
