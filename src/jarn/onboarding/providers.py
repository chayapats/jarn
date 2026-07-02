"""Shared onboarding helpers for provider classification."""

from __future__ import annotations

from jarn.config.defaults import CLOUD_PROVIDERS, CUSTOM_OPENAI_PROFILE


def provider_hint(name: str) -> str:
    """Return the provider class label: cloud, local, or custom."""
    if name == CUSTOM_OPENAI_PROFILE:
        return "custom"
    if name in CLOUD_PROVIDERS:
        return "cloud"
    return "local"
