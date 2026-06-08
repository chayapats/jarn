"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarn.config.schema import (
    BudgetConfig,
    Config,
    PermissionMode,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
)


@pytest.fixture
def base_config() -> Config:
    return Config(
        default_profile="openrouter",
        permission_mode=PermissionMode.ASK,
        providers={
            "openrouter": ProviderConfig(
                type=ProviderType.OPENROUTER, api_key="sk-test",
                base_url="http://localhost:9999/v1",
            ),
            "ollama": ProviderConfig(type=ProviderType.OLLAMA, base_url="http://localhost:11434"),
        },
        routing=RoutingConfig(
            main="openrouter/anthropic/claude-opus-4-8",
            subagent="openrouter/anthropic/claude-haiku-4-5",
        ),
        budget=BudgetConfig(per_session_usd=1.0, warn_at_pct=80, hard_stop=True),
    )


@pytest.fixture
def isolated_home(tmp_path, monkeypatch) -> Path:
    """Point JARN_HOME at a temp dir so tests never touch the real ~/.jarn."""
    home = tmp_path / "jarn-home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path) -> Path:
    """A temp project root with a .jarn marker directory."""
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    return root
