"""Tests for wizard-key-capture — the TUI wizard must capture, recommend, and
verify an API key before finishing (parity with the plain-text wizard).

Covers:
- env-present path: OPENCODE_API_KEY set → OpenCode is ★ recommended and
  default-highlighted; choosing it stores the ``${ENV}`` reference (never inline).
- env-absent path: a cloud provider with no resolvable key prompts for the key
  (keychain) before reaching the confirm screen.
- the recommendation/detection logic is reused from the plain wizard (no fork).
"""

from __future__ import annotations

import pytest

from jarn.catalog import CatalogSource, ModelCatalogEntry, ModelCatalogSnapshot
from jarn.config.defaults import CLOUD_PROVIDERS, PROVIDER_ENV_VARS


@pytest.fixture(autouse=True)
def _catalog_fixture(monkeypatch):
    def load(provider: str, **_kwargs):
        model = {
            "opencode": "glm-5.2",
            "anthropic": "claude-live",
            "openai": "gpt-live",
            "google": "gemini-live",
        }.get(provider, "provider-live-model")
        return ModelCatalogSnapshot(
            provider_profile=provider,
            provider_type=provider,
            source=CatalogSource.PROVIDER_LIVE,
            retrieved_at="2026-08-09T00:00:00Z",
            ttl_seconds=3600,
            expires_at="2026-08-09T01:00:00Z",
            stale=False,
            account_fingerprint="fixture",
            models=(
                ModelCatalogEntry(
                    provider_profile=provider,
                    model_id=model,
                    ref=f"{provider}/{model}",
                    display_name=model,
                    is_default=True,
                    account_available=True,
                ),
            ),
            availability_verified=True,
            provenance_label=f"Live {provider} fixture catalog",
        )

    monkeypatch.setattr("jarn.onboarding.tui_wizard.load_setup_catalog", load)


def _clear_provider_env(monkeypatch) -> None:
    for ev in PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(ev, raising=False)


async def _choose_provider(pilot, app, provider: str) -> None:
    """Navigate the simple first screen and, when needed, its detail screen."""

    top = app.query_one("#step-list")
    if provider == "opencode":
        top.highlighted = 1
        await pilot.press("enter")
        await pilot.pause()
        return
    if provider in ("ollama", "lmstudio"):
        top.highlighted = 3
    elif provider in CLOUD_PROVIDERS and provider != "openai_compatible":
        top.highlighted = 2
    else:
        top.highlighted = 4
    await pilot.press("enter")
    await pilot.pause()
    assert app.step == "provider_detail"
    detail = app.query_one("#step-list")
    detail.highlighted = next(
        idx for idx, option in enumerate(detail._options) if option.id == f"opt:{provider}"
    )
    await pilot.press("enter")
    await pilot.pause()


# ---------------------------------------------------------------------------
# env-present: detected key is offered + recommended, stored as a reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommended_provider_is_default_highlighted_when_opencode_key_set(
    tmp_path, monkeypatch
):
    """With OPENCODE_API_KEY set, OpenCode is tagged recommended and the
    provider list opens with OpenCode highlighted (not index 0)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode-detected")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    assert app.recommended == "opencode"
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        assert app.step == "provider"
        ol = app.query_one("#step-list")
        assert ol.highlighted == 1
        # The recommended tag is rendered somewhere in the option list.
        rendered = "\n".join(str(opt.prompt) for opt in ol._options)
        assert "recommended" in rendered


@pytest.mark.asyncio
async def test_choosing_detected_opencode_stores_env_reference_not_secret(tmp_path, monkeypatch):
    """Selecting the detected provider stores ``${OPENCODE_API_KEY}`` (a
    reference) and never inlines the secret — and skips straight past key entry."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode-detected-secret")
    from jarn.config import paths
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        # provider list opens highlighted on opencode (recommended) → select it.
        await pilot.press("enter")
        await pilot.pause()
        assert app.answers["provider"] == "opencode"
        # Detected env key → no key/storage prompt; key_ref already set.
        assert app.answers.get("key_ref") == "${OPENCODE_API_KEY}"
        # Walk to the end (model → theme → confirm → save).
        while app.step != "confirm":
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("enter")  # save
        await pilot.pause()

    # The Textual surface only stages; the shared outer completion gate commits.
    assert app._saved_config["default_profile"] == "opencode"
    assert app._saved_config["providers"]["opencode"]["api_key"] == "${OPENCODE_API_KEY}"
    assert not paths.global_config_path().exists()
    assert "sk-opencode-detected-secret" not in repr(app._saved_config)


# ---------------------------------------------------------------------------
# env-absent: a cloud provider with no resolvable key prompts before confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_provider_without_key_prompts_before_confirm(tmp_path, monkeypatch):
    """Choosing a cloud provider whose env var is unset and selecting the env
    storage option must NOT reach confirm with an unresolvable key — the wizard
    routes to the key-paste step first."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    from jarn.config import paths
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        # Pick openai (cloud, no env key set).
        await _choose_provider(pilot, app, "openai")
        assert app.step == "storage"
        await pilot.press("enter")  # env (first) → should detect missing key
        await pilot.pause()
        # No resolvable key for OPENAI → must land on the key-paste step,
        # NOT skip ahead to model/confirm.
        assert app.step == "key"
        inp = app.query_one("#step-input")
        inp.value = "sk-openai-pasted"
        await pilot.press("enter")
        await pilot.pause()
        assert app.pending_credentials.contains("openai")
        assert app.answers["_credential_pending"] == "memory"
        # Continue to the end.
        while app.step != "confirm":
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("enter")  # save
        await pilot.pause()

    assert app.pending_credentials.get("openai") == "sk-openai-pasted"
    assert not paths.global_config_path().exists()
    state_text = (paths.global_home() / "setup-state.json").read_text(encoding="utf-8")
    assert "sk-openai-pasted" not in state_text


@pytest.mark.asyncio
async def test_cloud_provider_with_env_key_does_not_prompt(tmp_path, monkeypatch):
    """When the chosen cloud provider's env var IS set, the detected key is
    reused as a reference and the wizard never forces key entry."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-env")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _choose_provider(pilot, app, "openai")
        # Detected env key → storage prompt is skipped; reference already set.
        assert app.step != "key"
        assert app.step != "storage"
        assert app.answers["key_ref"] == "${OPENAI_API_KEY}"


@pytest.mark.asyncio
async def test_non_detected_cloud_provider_with_env_key_keeps_reference(tmp_path, monkeypatch):
    """A cloud provider whose env var is set but that was NOT the top
    auto-detected hit still resolves its ${ENV} reference and skips key entry."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    # OpenCode wins detection; Google also has a key set.
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    assert app.env_hit == ("opencode", "OPENCODE_API_KEY")
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _choose_provider(pilot, app, "google")
        assert app.step == "storage"
        await pilot.press("enter")  # env → GOOGLE_API_KEY resolves, skip key step
        await pilot.pause()
        assert app.step != "key"
        assert app.answers["key_ref"] == "${GOOGLE_API_KEY}"


@pytest.mark.asyncio
async def test_local_provider_never_prompts_for_key(tmp_path, monkeypatch):
    """Local providers must still skip key capture entirely (no regression)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _choose_provider(pilot, app, "ollama")
        assert app.step == "base_url"
        assert "key_ref" not in app.answers


@pytest.mark.asyncio
async def test_tui_defers_secret_storage_until_shared_commit_gate(tmp_path, monkeypatch):
    """Paste stays in process memory; no keychain/file mutation happens in the TUI."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)

    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _choose_provider(pilot, app, "openai_compatible")
        await pilot.press("enter")  # storage: env → missing key → key step
        await pilot.pause()
        assert app.step == "key"
        inp = app.query_one("#step-input")
        inp.value = "sk-pi"
        await pilot.press("enter")
        await pilot.pause()
        assert app.pending_credentials.get("openai_compatible") == "sk-pi"
        assert app.answers["_credential_pending"] == "memory"
        assert not (tmp_path / "home" / "secrets").exists()
