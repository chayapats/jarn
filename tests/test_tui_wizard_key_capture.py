"""Tests for wizard-key-capture — the TUI wizard must capture, recommend, and
verify an API key before finishing (parity with the plain-text wizard).

Covers:
- env-present path: ANTHROPIC_API_KEY set → Anthropic is ★ recommended and
  default-highlighted; choosing it stores the ``${ENV}`` reference (never inline).
- env-absent path: a cloud provider with no resolvable key prompts for the key
  (keychain) before reaching the confirm screen.
- the recommendation/detection logic is reused from the plain wizard (no fork).
"""

from __future__ import annotations

import pytest

from jarn.config.defaults import ALL_PROVIDERS, PROVIDER_ENV_VARS


def _clear_provider_env(monkeypatch) -> None:
    for ev in PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(ev, raising=False)


# ---------------------------------------------------------------------------
# env-present: detected key is offered + recommended, stored as a reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommended_provider_is_default_highlighted_when_anthropic_key_set(
    tmp_path, monkeypatch
):
    """With ANTHROPIC_API_KEY set, Anthropic is tagged recommended and the
    provider list opens with Anthropic highlighted (not index 0)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-detected")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    assert app.recommended == "anthropic"
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        assert app.step == "provider"
        anthropic_idx = list(ALL_PROVIDERS).index("anthropic")
        ol = app.query_one("#step-list")
        assert ol.highlighted == anthropic_idx
        # The recommended tag is rendered somewhere in the option list.
        rendered = "\n".join(str(opt.prompt) for opt in ol._options)
        assert "recommended" in rendered


@pytest.mark.asyncio
async def test_choosing_detected_anthropic_stores_env_reference_not_secret(
    tmp_path, monkeypatch
):
    """Selecting the detected provider stores ``${ANTHROPIC_API_KEY}`` (a
    reference) and never inlines the secret — and skips straight past key entry."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-detected-secret")
    from jarn.config import paths
    from jarn.config.loader import load_config
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        # provider list opens highlighted on anthropic (recommended) → select it.
        await pilot.press("enter")
        await pilot.pause()
        assert app.answers["provider"] == "anthropic"
        # Detected env key → no key/storage prompt; key_ref already set.
        assert app.answers.get("key_ref") == "${ANTHROPIC_API_KEY}"
        # Walk to the end (model → theme → confirm → save).
        while app.step != "confirm":
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("enter")  # save
        await pilot.pause()

    cfg = load_config()
    assert cfg.default_profile == "anthropic"
    assert cfg.providers["anthropic"].api_key == "${ANTHROPIC_API_KEY}"
    # The literal secret must never be written to disk.
    written = paths.global_config_path().read_text(encoding="utf-8")
    assert "sk-ant-detected-secret" not in written


# ---------------------------------------------------------------------------
# env-absent: a cloud provider with no resolvable key prompts before confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_provider_without_key_prompts_before_confirm(
    tmp_path, monkeypatch
):
    """Choosing a cloud provider whose env var is unset and selecting the env
    storage option must NOT reach confirm with an unresolvable key — the wizard
    routes to the key-paste step first."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "jarn.onboarding.tui_wizard.store_keychain",
        lambda service, account, value: stored.__setitem__((service, account), value),
    )
    from jarn.config import paths
    from jarn.config.loader import load_config
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        # Pick openai (cloud, no env key set).
        openai_idx = list(ALL_PROVIDERS).index("openai")
        ol = app.query_one("#step-list")
        ol.highlighted = openai_idx
        await pilot.press("enter")  # provider → storage
        await pilot.pause()
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
        assert app.answers["key_ref"] == "keychain:jarn/openai"
        # Continue to the end.
        while app.step != "confirm":
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("enter")  # save
        await pilot.pause()

    assert stored[("jarn", "openai")] == "sk-openai-pasted"
    cfg = load_config()
    assert cfg.providers["openai"].api_key == "keychain:jarn/openai"
    written = paths.global_config_path().read_text(encoding="utf-8")
    assert "sk-openai-pasted" not in written


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
        openai_idx = list(ALL_PROVIDERS).index("openai")
        ol = app.query_one("#step-list")
        ol.highlighted = openai_idx
        await pilot.press("enter")  # provider: openai (env key detected)
        await pilot.pause()
        # Detected env key → storage prompt is skipped; reference already set.
        assert app.step != "key"
        assert app.step != "storage"
        assert app.answers["key_ref"] == "${OPENAI_API_KEY}"


@pytest.mark.asyncio
async def test_non_detected_cloud_provider_with_env_key_keeps_reference(
    tmp_path, monkeypatch
):
    """A cloud provider whose env var is set but that was NOT the top
    auto-detected hit still resolves its ${ENV} reference and skips key entry."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    # Anthropic wins detection; Google also has a key set.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    assert app.env_hit == ("anthropic", "ANTHROPIC_API_KEY")
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        google_idx = list(ALL_PROVIDERS).index("google")
        ol = app.query_one("#step-list")
        ol.highlighted = google_idx
        await pilot.press("enter")  # provider → storage (not the detected hit)
        await pilot.pause()
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
        ollama_idx = list(ALL_PROVIDERS).index("ollama")
        ol = app.query_one("#step-list")
        ol.highlighted = ollama_idx
        await pilot.press("enter")
        await pilot.pause()
        assert app.step == "base_url"
        assert "key_ref" not in app.answers
