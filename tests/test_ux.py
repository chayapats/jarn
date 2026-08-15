"""UX additions: mode cycling, model choices, and the setup wizard."""

from __future__ import annotations

import pytest

from jarn.catalog import CatalogError, CatalogSource, ModelCatalogEntry, ModelCatalogSnapshot
from jarn.config.schema import (
    Config,
    PermissionMode,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
)
from jarn.tui.controller import Controller


def _setup_snapshot(
    models: tuple[str, ...],
    *,
    provider: str = "ollama",
    verified: bool = True,
) -> ModelCatalogSnapshot:
    return ModelCatalogSnapshot(
        provider_profile=provider,
        provider_type=provider,
        source=CatalogSource.LOCAL_LIVE if verified else CatalogSource.STATIC_FALLBACK,
        retrieved_at="2026-08-09T00:00:00Z",
        ttl_seconds=3600,
        expires_at="2026-08-09T01:00:00Z",
        stale=False,
        account_fingerprint="fixture" if verified else None,
        models=tuple(
            ModelCatalogEntry(
                provider_profile=provider,
                model_id=model,
                ref=f"{provider}/{model}",
                display_name=model,
                account_available=True if verified else None,
                supports_tools=(True if provider == "ollama" and verified else None),
            )
            for model in models
        ),
        availability_verified=verified,
        provenance_label=(
            "Live local fixture catalog"
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
def _setup_catalog_fixture(monkeypatch):
    def load(provider: str, **_kwargs):
        if provider == "openai_compatible":
            return _setup_snapshot(("your-model",), provider=provider, verified=False)
        models = {
            "openrouter": ("anthropic/claude-opus-4.8",),
            "ollama": ("qwen3-coder:30b",),
            "lmstudio": ("qwen3-coder-30b",),
        }.get(provider, ("provider-live-model",))
        return _setup_snapshot(models, provider=provider)

    monkeypatch.setattr("jarn.onboarding.tui_wizard.load_setup_catalog", load)


def _ctrl(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={
            "openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x"),
            "google": ProviderConfig(type=ProviderType.GOOGLE, api_key="g"),
        },
        routing=RoutingConfig(
            main="openrouter/anthropic/claude-opus-4-8", fallback=["openrouter/openai/gpt-5.4"]
        ),
    )
    return Controller(cfg, root)


def test_cycle_mode_wraps(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch)
    assert ctrl.config.permission_mode is PermissionMode.ASK
    assert ctrl.cycle_mode() == "auto-edit"
    assert ctrl.cycle_mode() == "yolo"
    assert ctrl.cycle_mode() == "plan"
    assert ctrl.cycle_mode() == "ask"
    ctrl.close()


def test_model_choices_do_not_invent_unrefreshed_provider_defaults(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch)
    choices = dict(ctrl.model_choices())
    assert "openrouter/anthropic/claude-opus-4-8" in choices  # current
    assert "openrouter/openai/gpt-5.4" in choices  # fallback
    assert not any("gemini" in ref for ref in choices)
    assert all("availability unverified" in hint for hint in choices.values())
    ctrl.close()


def test_apply_model_resets_fallback_candidates(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch)
    ctrl.rotate_to_fallback()  # move off primary
    ctrl.apply_model("openrouter/custom/model")
    assert ctrl.config.routing.main == "openrouter/custom/model"
    assert ctrl._candidate_idx == 0
    ctrl.close()


def test_mode_choices_cover_all_modes(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch)
    keys = [k for k, _ in ctrl.mode_choices()]
    assert keys == ["plan", "ask", "auto-edit", "yolo"]
    ctrl.close()


# -- onboarding wizard (still Textual) -------------------------------------


def _option_index(option_list, key: str) -> int:
    return next(idx for idx, option in enumerate(option_list._options) if option.id == f"opt:{key}")


async def _select_setup_provider(pilot, app, provider: str, *, group: str) -> None:
    """Drive the simple provider screen into one concrete advanced provider."""

    first = app.query_one("#step-list")
    first.highlighted = _option_index(first, f"__{group}__")
    await pilot.press("enter")
    await pilot.pause()
    assert app.step == "provider_detail"
    detail = app.query_one("#step-list")
    detail.highlighted = _option_index(detail, provider)
    await pilot.press("enter")
    await pilot.pause()


async def _accept_advanced_defaults(pilot, app) -> None:
    """Walk every advanced control with its visible default."""

    assert app.step == "reasoning"
    await pilot.press("enter")  # provider/model default reasoning
    await pilot.pause()
    for expected in (
        "subagent_model",
        "summarizer_model",
        "fallback_models",
        "budget",
        "budget_warn",
    ):
        assert app.step == expected
        await pilot.press("enter")
        await pilot.pause()
    assert app.step == "budget_stop"
    await pilot.press("enter")  # hard stop
    await pilot.pause()
    assert app.step == "permissions"
    # Default highlight is Ask before changes, regardless of item order.
    await pilot.press("enter")
    await pilot.pause()
    assert app.step == "theme"


@pytest.mark.asyncio
async def test_setup_wizard_branches_local_skips_key(tmp_path, monkeypatch):
    """Picking a local provider must skip the key/storage steps (branching)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        assert app.step == "provider"
        await _select_setup_provider(pilot, app, "ollama", group="local")
        # Local provider → base_url (editable), then model — no storage/key steps.
        assert app.step == "base_url"
        assert app.answers["provider"] == "ollama"
        await pilot.press("enter")  # accept default Ollama URL
        await pilot.pause()
        assert app.step == "model"


def _clear_provider_env(monkeypatch) -> None:
    from jarn.config.defaults import PROVIDER_ENV_VARS

    for ev in PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(ev, raising=False)


@pytest.mark.asyncio
async def test_setup_wizard_full_flow_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    # The env key is present, so the env reference resolves and the wizard does
    # not have to stop to capture a key (openrouter becomes the detected hit).
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    from jarn.config import paths
    from jarn.onboarding.tui_wizard import SetupApp

    async def step(pilot, key):
        await pilot.press("enter")
        await pilot.pause()

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        # openrouter is the detected env hit + recommended → selecting it reuses
        # the ${ENV} reference and skips straight past the storage prompt.
        await step(pilot, "provider")  # another cloud → provider detail
        assert app.step == "provider_detail"
        await step(pilot, "provider_detail")  # default model selected; storage skipped
        assert app.step == "theme"
        await step(pilot, "theme")  # dark (first) → confirm
        assert app.step == "confirm"
        await step(pilot, "confirm")  # save (first)

    assert app.result_path == paths.global_config_path()
    assert not paths.global_config_path().exists()
    assert app._saved_config["default_profile"] == "openrouter"
    assert app._saved_config["permission_mode"] == "ask"
    assert app._saved_config["providers"]["openrouter"]["api_key"] == "${OPENROUTER_API_KEY}"


@pytest.mark.asyncio
async def test_wizard_openrouter_with_slashed_model(tmp_path, monkeypatch):
    """Regression: provider=openrouter + model 'deepseek/deepseek-v4-flash'
    must route through openrouter, not the deepseek provider."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        # Manual model ids live under Advanced; the standard path auto-selects.
        await _select_setup_provider(pilot, app, "openrouter", group="advanced")
        # model step: a curated cloud pick-list — drop to the custom entry and
        # type an OpenRouter model id that contains a slash.
        ol = app.query_one("#step-list")
        ol.highlighted = len(ol._options) - 1  # the "enter manually" entry
        await pilot.press("enter")
        await pilot.pause()
        inp = app.query_one("#step-input")
        inp.value = "deepseek/deepseek-v4-flash"
        await pilot.press("enter")
        await pilot.pause()
        assert app.answers["model"] == "openrouter/deepseek/deepseek-v4-flash"
        await _accept_advanced_defaults(pilot, app)
        await pilot.press("enter")  # theme
        await pilot.pause()
        await pilot.press("enter")  # confirm: save
        await pilot.pause()

    assert app._saved_config["routing"]["main"] == "openrouter/deepseek/deepseek-v4-flash"
    # And it resolves to the openrouter provider (not deepseek).
    from jarn.providers import parse_model_ref

    assert parse_model_ref(app._saved_config["routing"]["main"]).profile == "openrouter"


@pytest.mark.asyncio
async def test_setup_wizard_openai_compatible_custom_endpoint(tmp_path, monkeypatch):
    """Custom OpenAI-compatible: key + base_url + model are all persisted."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _clear_provider_env(monkeypatch)
    # env reference resolves so the env storage path proceeds without a key prompt.
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "sk-compat-env")
    from jarn.config import paths
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await _select_setup_provider(
            pilot,
            app,
            "openai_compatible",
            group="advanced",
        )
        # openai_compatible's env var is set → detected hit reuses the ${ENV}
        # reference and skips the storage prompt, landing on base_url.
        assert app.step == "base_url"
        inp = app.query_one("#step-input")
        inp.value = "https://proxy.example.com"
        await pilot.press("enter")
        await pilot.pause()
        assert app.answers["base_url"] == "https://proxy.example.com/v1"
        assert app.step == "model"
        inp = app.query_one("#step-input")
        inp.value = "qwen3-coder"
        await pilot.press("enter")
        await pilot.pause()
        assert app.answers["model"] == "openai_compatible/qwen3-coder"
        await _accept_advanced_defaults(pilot, app)
        await pilot.press("enter")  # theme
        await pilot.pause()
        await pilot.press("enter")  # confirm save
        await pilot.pause()

    assert app.result_path == paths.global_config_path()
    assert not paths.global_config_path().exists()
    assert app._saved_config["default_profile"] == "openai_compatible"
    assert app._saved_config["permission_mode"] == "ask"
    prov = app._saved_config["providers"]["openai_compatible"]
    assert prov["api_key"] == "${OPENAI_COMPATIBLE_API_KEY}"
    assert prov["base_url"] == "https://proxy.example.com/v1"
    assert app._saved_config["routing"]["main"] == "openai_compatible/qwen3-coder"


def test_normalize_openai_base_url():
    from jarn.onboarding.wizard import normalize_base_url, normalize_openai_base_url

    assert normalize_openai_base_url("https://api.example.com") == "https://api.example.com/v1"
    assert normalize_openai_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    assert normalize_openai_base_url("http://localhost:8000/v1") == "http://localhost:8000/v1"
    assert normalize_base_url("ollama", "http://localhost:11434") == "http://localhost:11434"
    assert normalize_base_url("lmstudio", "http://127.0.0.1:1234") == "http://127.0.0.1:1234/v1"


def test_derive_routing_models_openai_compatible():
    from jarn.onboarding.wizard import derive_routing_models

    main = "openai_compatible/qwen3-coder"
    routing = derive_routing_models("openai_compatible", main)
    assert routing["main"] == main
    assert routing["subagent"] == main
    assert routing["summarizer"] == main


def test_configure_ui_switches_palette():
    from jarn.tui import palette

    palette.configure_ui(theme="light")
    assert palette.C_USER == "#0e7490"
    palette.configure_ui(theme="dark")
    assert palette.C_USER == "#38e1ff"


def test_setup_cancel_returns_exit_130_with_stable_anatomy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("jarn.onboarding.run_setup_tui", lambda **kw: None)
    from jarn.cli import _cmd_setup

    assert _cmd_setup() == 130
    error = capsys.readouterr().err
    assert "JARN-CLI-002" in error
    assert "Cause:" in error
    assert "Component:" in error
    assert "Next:" in error
    assert "Log:" in error


def test_confirm_overwrite_decline_keeps_config(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.config import paths
    from jarn.onboarding.wizard import confirm_overwrite, run_wizard

    paths.global_home().mkdir(parents=True)
    paths.global_config_path().write_text("existing: true\n", encoding="utf-8")
    monkeypatch.setattr("jarn.onboarding.wizard.Confirm.ask", lambda *a, **k: False)
    assert confirm_overwrite() is False
    assert run_wizard() is None
    assert "existing: true" in paths.global_config_path().read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_setup_wizard_back_navigation(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        cloud = app.query_one("#step-list")
        cloud.highlighted = _option_index(cloud, "__cloud__")
        await pilot.press("enter")  # provider → cloud detail
        await pilot.pause()
        assert app.step == "provider_detail"
        await pilot.press("escape")  # back → simple provider screen
        await pilot.pause()
        assert app.step == "provider"


async def _goto_ollama_model_step(pilot, app):
    """Drive the wizard provider→base_url→model for the local 'ollama' provider."""
    await _select_setup_provider(pilot, app, "ollama", group="local")
    assert app.step == "base_url"
    await pilot.press("enter")  # accept default Ollama URL → model
    await pilot.pause()
    assert app.step == "model"


@pytest.mark.asyncio
async def test_wizard_model_step_offers_arrow_select_when_endpoint_reachable(tmp_path, monkeypatch):
    """A reachable Ollama endpoint → the model step is an arrow-key OptionList."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from unittest.mock import patch

    from textual.widgets import OptionList

    from jarn.onboarding.tui_wizard import SetupApp

    # Mock the unified catalog so no real network call happens.
    with patch(
        "jarn.onboarding.tui_wizard.load_setup_catalog",
        return_value=_setup_snapshot(("qwen3-coder:30b", "llama3:8b")),
    ):
        app = SetupApp()
        async with app.run_test(size=(90, 40)) as pilot:
            await pilot.pause()
            await _goto_ollama_model_step(pilot, app)
            # The model step is now a selectable list, not a free-text Input.
            lst = app.query_one("#step-list", OptionList)
            labels = [str(opt.prompt) for opt in lst._options]
            assert any("qwen3-coder:30b" in lbl for lbl in labels)
            assert any("llama3:8b" in lbl for lbl in labels)
            assert any("manually" in lbl.lower() for lbl in labels)
            # Pick the first discovered model via arrow-select.
            lst.highlighted = 0
            await pilot.press("enter")
            await pilot.pause()
    assert app.answers["model"] == "ollama/qwen3-coder:30b"


@pytest.mark.asyncio
async def test_wizard_model_step_degrades_to_manual_when_unreachable(tmp_path, monkeypatch):
    """An unreachable endpoint (empty list) → free-text Input, never blocks."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from unittest.mock import patch

    from textual.widgets import Input

    from jarn.onboarding.tui_wizard import SetupApp

    with patch(
        "jarn.onboarding.tui_wizard.load_setup_catalog",
        return_value=_setup_snapshot(("qwen3-coder:30b",), verified=False),
    ):
        app = SetupApp()
        async with app.run_test(size=(90, 40)) as pilot:
            await pilot.pause()
            await _goto_ollama_model_step(pilot, app)
            # No discovered models → manual entry input is present.
            inp = app.query_one("#step-input", Input)
            inp.value = "my-local-model"
            await pilot.press("enter")
            await pilot.pause()
    assert app.answers["model"] == "ollama/my-local-model"


def test_blank_line_rhythm():
    """Spy-based test: _sep emits 0 blanks for same-kind (text→text) and 1+ for
    kind transitions (reasoning→text).  Goes RED under the old _sep logic.

    OLD _sep: ``if not (_prev == "tool" and kind == "tool"): console.print()``
        → always emits a blank for text→text (non-tool→non-tool always fires)
    NEW _sep: ``if _prev != kind: console.print()``
        → suppresses the blank when the kind does not change

    Rationale: exactly one separator blank belongs between committed blocks of
    DIFFERENT kinds; consecutive same-kind commits get none.  No production path
    commits same-kind text consecutively, so the same-kind suppression only
    removes the spurious extra blank the old rule emitted.  The spy counts
    ``console.print()`` calls with NO arguments (the blank-line signal) directly,
    since a blank line is not distinguishable in StringIO buffer content.
    """
    from io import StringIO

    from rich.console import Console

    from jarn.repl_renderer import TurnRenderer

    def _renderer_with_spy():
        c = Console(file=StringIO(), width=80)
        calls: list[tuple] = []
        orig = c.print

        def spy(*args, **kwargs):  # noqa: E306
            calls.append(args)
            orig(*args, **kwargs)

        c.print = spy  # type: ignore[method-assign]
        return TurnRenderer(c, spinner=False), calls

    # ── A: text → text ───────────────────────────────────────────────────────
    # _sep("text") fires for first commit (prev=None → blank expected).
    # _sep("text") fires for second commit: NEW → no blank; OLD → blank.
    r_a, calls_a = _renderer_with_spy()
    r_a.on_text("first para\n\n")
    mark_a = len(calls_a)  # snapshot after first commit
    r_a.on_text("second para\n\n")
    r_a.finish()

    blanks_between = sum(1 for a in calls_a[mark_a:] if a == ())
    assert blanks_between == 0, (
        f"text→text: 0 separator blanks expected between commits, "
        f"got {blanks_between}. Old _sep() always emitted 1 here (regression)."
    )

    # ── B: reasoning → text (transition blank must fire) ────────────────────
    # on_text triggers _commit_reasoning (_sep("reasoning"), prev=None → 1 blank)
    # then _flush_stable (_sep("text"), prev="reasoning" → 1 transition blank).
    r_b, calls_b = _renderer_with_spy()
    r_b.on_reasoning("thinking…")
    mark_b = len(calls_b)  # snapshot after on_reasoning (no sep fired yet)
    r_b.on_text("answer\n\n")
    r_b.finish()

    blanks_in_transition = sum(1 for a in calls_b[mark_b:] if a == ())
    assert blanks_in_transition >= 1, (
        f"reasoning→text: at least 1 transition blank expected, got {blanks_in_transition}"
    )


def test_no_color_styled_fg():
    """With NO_COLOR active, styled_fg returns plain text for both bold and non-bold."""
    import os

    from jarn.tui.palette import styled_fg

    old = os.environ.get("NO_COLOR")
    try:
        os.environ["NO_COLOR"] = "1"
        plain = styled_fg("#ff0000", "hello")
        bold_plain = styled_fg("#ff0000", "hello", bold=True)
    finally:
        if old is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = old

    assert plain == "hello"
    assert bold_plain == "hello"
    # No HTML markup or ANSI escapes leak through
    assert "<style" not in bold_plain
    assert "<b>" not in bold_plain


def test_strip_md_wrappers_keeps_fences_and_lists():
    from jarn.tui import layout

    src = "A **bold** word and __also__ this.\n\n```\nkeep **stars**\n```\n\n- list item\n1. numbered"
    out = layout.strip_md_wrappers(src)
    assert "**bold**" not in out
    assert "__also__" not in out
    assert "bold" in out and "also" in out
    assert "keep **stars**" in out
    assert "- list item" in out
    assert "1. numbered" in out
