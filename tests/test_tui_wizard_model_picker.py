"""Tests for wizard-model-picker — the model step must not be a blind free-text
box.

Covers:
- cloud providers: a curated arrow-key model pick-list (from DEFAULT_MODELS) is
  offered, plus a "custom" entry that drops to free-text — and picking a listed
  model qualifies it under the chosen provider.
- a typed cloud slug with the wrong dot/dash form surfaces an inline
  ``suggest_slug`` hint instead of silently advancing.
- an unreachable local endpoint shows a "is your server running?" nudge before
  the manual-entry box (no silent blind-type box).

All network / discovery is mocked — no live endpoints.
"""

from __future__ import annotations

import pytest

from jarn.config.defaults import ALL_PROVIDERS


def _clear_provider_env(monkeypatch) -> None:
    from jarn.config.defaults import PROVIDER_ENV_VARS

    for ev in PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(ev, raising=False)


def _rendered(option_list) -> str:
    return "\n".join(str(opt.prompt) for opt in option_list._options)


# ---------------------------------------------------------------------------
# cloud provider: curated pick-list + custom fallback
# ---------------------------------------------------------------------------


def test_curated_cloud_models_dedup_and_strip_profile() -> None:
    """The curated list for a cloud provider is built from DEFAULT_MODELS,
    deduplicated, with the leading profile stripped (what the user would type)."""
    from jarn.onboarding.tui_wizard import _curated_cloud_models

    items = _curated_cloud_models("anthropic")
    assert "claude-opus-4-8" in items
    # haiku appears twice (subagent + summarizer) but must be deduplicated.
    assert items.count("claude-haiku-4-5") == 1
    # profile prefix is stripped for OpenRouter's nested vendor refs.
    or_items = _curated_cloud_models("openrouter")
    assert any(m.startswith("anthropic/claude-opus-4.8") for m in or_items)
    assert all(not m.startswith("openrouter/") for m in or_items)


@pytest.mark.asyncio
async def test_cloud_provider_offers_model_picklist_with_custom_entry(
    tmp_path, monkeypatch
):
    """Choosing a cloud provider with a detected env key lands on the model step
    rendered as a selectable list that includes a custom free-text entry."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # anthropic (recommended) → straight to model
        await pilot.pause()
        assert app.step == "model"
        ol = app.query_one("#step-list")
        rendered = _rendered(ol)
        assert "claude-opus-4-8" in rendered
        assert "manually" in rendered.lower()  # custom free-text entry present


@pytest.mark.asyncio
async def test_cloud_picklist_selection_qualifies_model(tmp_path, monkeypatch):
    """Selecting a curated model qualifies it under the provider profile."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # anthropic → model
        await pilot.pause()
        assert app.step == "model"
        await pilot.press("enter")  # pick the highlighted (default) model
        await pilot.pause()
        assert app.step == "theme"
        assert app.answers["model"] == "anthropic/claude-opus-4-8"


@pytest.mark.asyncio
async def test_cloud_custom_entry_drops_to_freetext(tmp_path, monkeypatch):
    """The custom entry in the cloud pick-list drops to a free-text input."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # anthropic → model
        await pilot.pause()
        ol = app.query_one("#step-list")
        # highlight the last option (the custom/manual entry) and select it.
        ol.highlighted = len(ol._options) - 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.step == "model"
        inp = app.query_one("#step-input")  # free-text input now shown
        inp.value = "claude-sonnet-4-5"
        await pilot.press("enter")
        await pilot.pause()
        assert app.answers["model"] == "anthropic/claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# slug hint: a wrong dot/dash form surfaces a suggestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_slug_form_shows_suggest_slug_hint(tmp_path, monkeypatch):
    """Typing the dot-form slug for the dash-form Anthropic API surfaces the
    suggest_slug hint inline and does NOT advance to the theme step."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # anthropic → model
        await pilot.pause()
        ol = app.query_one("#step-list")
        ol.highlighted = len(ol._options) - 1  # custom entry
        await pilot.press("enter")
        await pilot.pause()
        inp = app.query_one("#step-input")
        inp.value = "claude-opus-4.8"  # WRONG: dot form, Anthropic wants dashes
        await pilot.press("enter")
        await pilot.pause()
        # The hint blocks advancing; we stay on the model step.
        assert app.step == "model"
        title = str(app.query_one("#title").render())
        assert "claude-opus-4-8" in title  # the corrected suggestion is shown


@pytest.mark.asyncio
async def test_slug_hint_can_be_overridden_by_resubmitting(tmp_path, monkeypatch):
    """After the hint, resubmitting the same value accepts it (the user's call)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        ol = app.query_one("#step-list")
        ol.highlighted = len(ol._options) - 1
        await pilot.press("enter")
        await pilot.pause()
        inp = app.query_one("#step-input")
        inp.value = "claude-opus-4.8"
        await pilot.press("enter")  # first submit → hint, stays
        await pilot.pause()
        assert app.step == "model"
        inp = app.query_one("#step-input")
        inp.value = "claude-opus-4.8"
        await pilot.press("enter")  # second submit of same value → accept
        await pilot.pause()
        assert app.step == "theme"
        assert app.answers["model"] == "anthropic/claude-opus-4.8"


# ---------------------------------------------------------------------------
# unreachable local endpoint: nudge, then manual entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_local_endpoint_shows_nudge(tmp_path, monkeypatch):
    """When discovery returns nothing for a local provider, the model step shows
    a 'is your server running?' nudge and still degrades to manual entry."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    # Mock discovery to simulate an unreachable endpoint (empty list).
    monkeypatch.setattr(
        "jarn.onboarding.tui_wizard.list_remote_models", lambda provider: []
    )
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        ollama_idx = list(ALL_PROVIDERS).index("ollama")
        ol = app.query_one("#step-list")
        ol.highlighted = ollama_idx
        await pilot.press("enter")  # provider → base_url
        await pilot.pause()
        assert app.step == "base_url"
        await pilot.press("enter")  # accept default base_url → model
        await pilot.pause()
        assert app.step == "model"
        title = str(app.query_one("#title").render())
        assert "couldn't reach" in title.lower() or "couldn’t reach" in title.lower()
        assert "ollama" in title.lower()
        # still a manual-entry box (degrades gracefully)
        inp = app.query_one("#step-input")
        inp.value = "qwen3-coder:30b"
        await pilot.press("enter")
        await pilot.pause()
        assert app.answers["model"] == "ollama/qwen3-coder:30b"


@pytest.mark.asyncio
async def test_reachable_local_endpoint_still_offers_picklist(tmp_path, monkeypatch):
    """A reachable local endpoint keeps the discovered pick-list (no regression)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        "jarn.onboarding.tui_wizard.list_remote_models",
        lambda provider: ["qwen3-coder:30b", "llama3:8b"],
    )
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        ollama_idx = list(ALL_PROVIDERS).index("ollama")
        ol = app.query_one("#step-list")
        ol.highlighted = ollama_idx
        await pilot.press("enter")  # provider → base_url
        await pilot.pause()
        await pilot.press("enter")  # base_url → model
        await pilot.pause()
        assert app.step == "model"
        ol = app.query_one("#step-list")
        rendered = _rendered(ol)
        assert "qwen3-coder:30b" in rendered
        assert "llama3:8b" in rendered
