"""UX additions: mode cycling, model choices, and the setup wizard."""

from __future__ import annotations

import pytest

from jarn.config.schema import (
    Config,
    PermissionMode,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
)
from jarn.tui.controller import Controller


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
        routing=RoutingConfig(main="openrouter/anthropic/claude-opus-4-8",
                              fallback=["openrouter/openai/gpt-5.4"]),
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


def test_model_choices_include_fallback_and_providers(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch)
    choices = dict(ctrl.model_choices())
    assert "openrouter/anthropic/claude-opus-4-8" in choices  # current
    assert "openrouter/openai/gpt-5.4" in choices             # fallback
    assert any("gemini" in ref for ref in choices)            # google default
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

@pytest.mark.asyncio
async def test_setup_wizard_branches_local_skips_key(tmp_path, monkeypatch):
    """Picking a local provider must skip the key/storage steps (branching)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        assert app.step == "provider"
        # Select "ollama" (local) by id, simulating an Enter on that option.
        ollama_idx = [p for p in __import__("jarn.config.defaults", fromlist=["ALL_PROVIDERS"]).ALL_PROVIDERS].index("ollama")
        ol = app.query_one("#step-list")
        ol.highlighted = ollama_idx
        await pilot.press("enter")
        await pilot.pause()
        # Local provider → base_url (editable), then model — no storage/key steps.
        assert app.step == "base_url"
        assert app.answers["provider"] == "ollama"
        await pilot.press("enter")          # accept default Ollama URL
        await pilot.pause()
        assert app.step == "model"


@pytest.mark.asyncio
async def test_setup_wizard_full_flow_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.config import paths
    from jarn.config.loader import load_config
    from jarn.onboarding.tui_wizard import SetupApp

    async def step(pilot, key):
        await pilot.press("enter")
        await pilot.pause()

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await step(pilot, "provider")   # openrouter (first) → storage
        assert app.step == "storage"
        await step(pilot, "storage")    # env (first) → model
        assert app.step == "model"
        await step(pilot, "model")      # accept prefilled default → theme
        assert app.step == "theme"
        await step(pilot, "theme")      # dark (first) → confirm
        assert app.step == "confirm"
        await step(pilot, "confirm")    # save (first)

    assert app.result_path == paths.global_config_path()
    cfg = load_config()
    assert cfg.default_profile == "openrouter"
    assert cfg.permission_mode.value == "ask"
    assert cfg.providers["openrouter"].api_key == "${OPENROUTER_API_KEY}"


@pytest.mark.asyncio
async def test_wizard_openrouter_with_slashed_model(tmp_path, monkeypatch):
    """Regression: provider=openrouter + model 'deepseek/deepseek-v4-flash'
    must route through openrouter, not the deepseek provider."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.config.loader import load_config
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")          # provider: openrouter (first)
        await pilot.pause()
        await pilot.press("enter")          # storage: env (first)
        await pilot.pause()
        # model step: type an OpenRouter model id that contains a slash
        inp = app.query_one("#step-input")
        inp.value = "deepseek/deepseek-v4-flash"
        await pilot.press("enter")
        await pilot.pause()
        assert app.answers["model"] == "openrouter/deepseek/deepseek-v4-flash"
        await pilot.press("enter")          # theme
        await pilot.pause()
        await pilot.press("enter")          # confirm: save
        await pilot.pause()

    cfg = load_config()
    assert cfg.resolved_main_model() == "openrouter/deepseek/deepseek-v4-flash"
    # And it resolves to the openrouter provider (not deepseek).
    from jarn.providers import parse_model_ref
    assert parse_model_ref(cfg.resolved_main_model()).profile == "openrouter"


@pytest.mark.asyncio
async def test_setup_wizard_openai_compatible_custom_endpoint(tmp_path, monkeypatch):
    """Custom OpenAI-compatible: key + base_url + model are all persisted."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.config import paths
    from jarn.config.defaults import ALL_PROVIDERS
    from jarn.config.loader import load_config
    from jarn.onboarding.tui_wizard import SetupApp

    compat_idx = list(ALL_PROVIDERS).index("openai_compatible")

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        ol = app.query_one("#step-list")
        ol.highlighted = compat_idx
        await pilot.press("enter")          # provider → storage
        await pilot.pause()
        assert app.step == "storage"
        await pilot.press("enter")          # env → base_url
        await pilot.pause()
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
        await pilot.press("enter")          # theme
        await pilot.pause()
        await pilot.press("enter")          # confirm save
        await pilot.pause()

    assert app.result_path == paths.global_config_path()
    cfg = load_config()
    assert cfg.default_profile == "openai_compatible"
    assert cfg.permission_mode.value == "ask"
    prov = cfg.providers["openai_compatible"]
    assert prov.api_key == "${OPENAI_COMPATIBLE_API_KEY}"
    assert prov.base_url == "https://proxy.example.com/v1"
    assert cfg.resolved_main_model() == "openai_compatible/qwen3-coder"


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


def test_apply_ui_theme_switches_palette():
    from jarn.tui import palette

    palette.apply_ui_theme("light")
    assert palette.C_USER == "#0e7490"
    palette.apply_ui_theme("dark")
    assert palette.C_USER == "#38e1ff"


def test_setup_cancel_returns_exit_one(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "jarn.onboarding.run_setup_tui", lambda **kw: None
    )
    from jarn.cli import _cmd_setup

    assert _cmd_setup() == 1


def test_confirm_overwrite_decline_keeps_config(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.config import paths
    from jarn.onboarding.wizard import confirm_overwrite, run_wizard

    paths.global_home().mkdir(parents=True)
    paths.global_config_path().write_text("existing: true\n", encoding="utf-8")
    monkeypatch.setattr(
        "jarn.onboarding.wizard.Confirm.ask", lambda *a, **k: False
    )
    assert confirm_overwrite() is False
    assert run_wizard() == paths.global_config_path()
    assert "existing: true" in paths.global_config_path().read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_setup_wizard_back_navigation(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")     # provider → storage
        await pilot.pause()
        assert app.step == "storage"
        await pilot.press("escape")    # back → provider
        await pilot.pause()
        assert app.step == "provider"
