"""Tests for the setup wizard's unified live model-catalog picker.

Covers:
- cloud providers: provider-reported models are offered, plus a manual entry;
- unverified static fallback is never rendered as an available choice;
- a typed cloud slug with the wrong dot/dash form surfaces an inline
  ``suggest_slug`` hint instead of silently advancing.
- an unreachable local endpoint shows a "is your server running?" nudge before
  the manual-entry box (no silent blind-type box).

All network / discovery is mocked — no live endpoints.
"""

from __future__ import annotations

import pytest

from jarn.catalog import (
    CatalogError,
    CatalogSource,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
)
from jarn.tui.i18n import t


def _snapshot(
    provider: str,
    models: tuple[str, ...],
    *,
    verified: bool = True,
    ollama_supports_tools: bool | None = True,
) -> ModelCatalogSnapshot:
    entries = tuple(
        ModelCatalogEntry(
            provider_profile=provider,
            model_id=model,
            ref=f"{provider}/{model}",
            display_name=model,
            is_default=index == 0,
            account_available=True if verified else None,
            supports_tools=(ollama_supports_tools if provider == "ollama" and verified else None),
        )
        for index, model in enumerate(models)
    )
    return ModelCatalogSnapshot(
        provider_profile=provider,
        provider_type=provider,
        source=CatalogSource.PROVIDER_LIVE if verified else CatalogSource.STATIC_FALLBACK,
        retrieved_at="2026-08-09T00:00:00Z",
        ttl_seconds=3600,
        expires_at="2026-08-09T01:00:00Z",
        stale=False,
        account_fingerprint="fixture" if verified else None,
        models=entries,
        availability_verified=verified,
        provenance_label=(
            f"Live {provider} fixture catalog"
            if verified
            else "Offline fallback; availability unverified"
        ),
        error=(
            None
            if verified
            else CatalogError("MODEL_CATALOG_UNAVAILABLE", "fixture endpoint unavailable")
        ),
    )


@pytest.fixture(autouse=True)
def _live_catalog(monkeypatch):
    def load(provider: str, **_kwargs):
        models = {
            "opencode": ("glm-5.2", "glm-5.1"),
            "anthropic": ("claude-opus-4-8", "claude-haiku-4-5"),
            "ollama": ("qwen3-coder:30b", "llama3:8b"),
            "lmstudio": ("qwen3-coder-30b",),
        }.get(provider, ("provider-live-model",))
        return _snapshot(provider, models)

    monkeypatch.setattr("jarn.onboarding.tui_wizard.load_setup_catalog", load)


def _clear_provider_env(monkeypatch) -> None:
    from jarn.config.defaults import PROVIDER_ENV_VARS

    for ev in PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(ev, raising=False)


def _rendered(option_list) -> str:
    return "\n".join(str(opt.prompt) for opt in option_list._options)


async def _choose_local_provider(pilot, app, provider: str) -> None:
    top = app.query_one("#step-list")
    top.highlighted = 3
    await pilot.press("enter")
    await pilot.pause()
    assert app.step == "provider_detail"
    detail = app.query_one("#step-list")
    detail.highlighted = next(
        idx for idx, option in enumerate(detail._options) if option.id == f"opt:{provider}"
    )
    await pilot.press("enter")
    await pilot.pause()


async def _choose_advanced_provider(pilot, app, provider: str) -> None:
    top = app.query_one("#step-list")
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
# cloud provider: live pick-list + manual fallback
# ---------------------------------------------------------------------------


def test_unverified_static_catalog_has_no_selectable_setup_models() -> None:
    from jarn.onboarding.model_catalog import selectable_setup_models

    assert (
        selectable_setup_models(_snapshot("anthropic", ("static-bootstrap",), verified=False)) == ()
    )


@pytest.mark.asyncio
async def test_standard_cloud_path_selects_supported_default_without_model_id_step(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # recommended OpenCode standard path
        await pilot.pause()
        assert app.step == "theme"
        assert app.answers["model"].startswith("opencode/")


@pytest.mark.asyncio
async def test_cloud_provider_offers_model_picklist_with_custom_entry(tmp_path, monkeypatch):
    """Choosing a cloud provider with a detected env key lands on the model step
    rendered as a selectable list that includes a custom free-text entry."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _choose_advanced_provider(pilot, app, "anthropic")
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
        await _choose_advanced_provider(pilot, app, "anthropic")
        assert app.step == "model"
        await pilot.press("enter")  # pick the highlighted (default) model
        await pilot.pause()
        assert app.step == "reasoning"
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
        await _choose_advanced_provider(pilot, app, "anthropic")
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
        await _choose_advanced_provider(pilot, app, "anthropic")
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
        await _choose_advanced_provider(pilot, app, "anthropic")
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
        assert app.step == "reasoning"
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
        "jarn.onboarding.tui_wizard.load_setup_catalog",
        lambda provider, **_kwargs: _snapshot(provider, ("qwen3-coder:30b",), verified=False),
    )
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _choose_local_provider(pilot, app, "ollama")
        assert app.step == "base_url"
        await pilot.press("enter")  # accept default base_url → model
        await pilot.pause()
        assert app.step == "model"
        title = str(app.query_one("#title").render())
        marker = t(
            "onboarding.model.catalog_unreachable",
            app.locale,
            endpoint="\x1e",
            status="\x1e",
        )
        assert marker.split("\x1e", 1)[0] in title
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
        "jarn.onboarding.tui_wizard.load_setup_catalog",
        lambda provider, **_kwargs: _snapshot(
            provider,
            ("qwen3-coder:30b", "llama3:8b"),
        ),
    )
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _choose_local_provider(pilot, app, "ollama")
        await pilot.press("enter")  # base_url → model
        await pilot.pause()
        assert app.step == "model"
        ol = app.query_one("#step-list")
        rendered = _rendered(ol)
        assert "qwen3-coder:30b" in rendered
        assert "llama3:8b" in rendered


@pytest.mark.asyncio
async def test_completion_only_ollama_model_is_not_offered_as_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        "jarn.onboarding.tui_wizard.load_setup_catalog",
        lambda provider, **_kwargs: _snapshot(
            provider,
            ("completion-only",),
            ollama_supports_tools=False,
        ),
    )
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _choose_local_provider(pilot, app, "ollama")
        await pilot.press("enter")  # base_url → model
        await pilot.pause()

        assert app.step == "model"
        title = str(app.query_one("#title").render()).lower()
        assert "no installed model has verified ollama tool support" in title
        assert "couldn't reach" not in title
        assert app.query_one("#step-input") is not None
