from __future__ import annotations

import json

import pytest

from jarn.catalog import CatalogSource, ModelCatalogEntry, ModelCatalogSnapshot
from jarn.onboarding.state import SetupState, load_setup_state


@pytest.fixture(autouse=True)
def _catalog_fixture(monkeypatch):
    def load(provider: str, **_kwargs):
        model = "claude-live" if provider == "anthropic" else "provider-live-model"
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


@pytest.mark.asyncio
async def test_textual_progress_is_resumable_and_never_persists_raw_key(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("jarn.onboarding.tui_wizard._detect_env_key", lambda: None)

    from jarn.onboarding.tui_wizard import SetupApp

    state_path = tmp_path / "setup-state.json"

    app = SetupApp(state_path=state_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        choices = app.query_one("#step-list")
        choices.highlighted = 1  # Anthropic
        await pilot.press("enter")
        await pilot.pause()
        assert app.step == "storage"
        storage = app.query_one("#step-list")
        storage.highlighted = 1  # paste/store
        await pilot.press("enter")
        await pilot.pause()
        assert app.step == "key"
        key = app.query_one("#step-input")
        key.value = "sk-ant-never-persist-this"
        await pilot.press("enter")
        await pilot.pause()

    payload = state_path.read_text(encoding="utf-8")
    assert "sk-ant-never-persist-this" not in payload
    assert '"_credential_pending":"memory"' in payload
    state = load_setup_state(path=state_path)
    assert state is not None
    assert state.stage == "theme"
    assert app.pending_credentials.get("anthropic") == "sk-ant-never-persist-this"


@pytest.mark.asyncio
async def test_textual_resume_opens_saved_stage_and_back_works_without_history(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.onboarding.tui_wizard import SetupApp

    resumed = SetupState(
        stage="theme",
        answers={
            "provider": "anthropic",
            "_provider_group": "standard",
            "key_ref": "${ANTHROPIC_API_KEY}",
            "model": "anthropic/claude-sonnet-4-5",
        },
        updated_at="2026-08-09T00:00:00Z",
    )
    state_path = tmp_path / "setup-state.json"
    app = SetupApp(resume_state=resumed, state_path=state_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.step == "theme"
        await pilot.press("escape")
        await pilot.pause()
        assert app.step == "storage"

    raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert raw["stage"] == "storage"


@pytest.mark.asyncio
async def test_textual_confirm_only_stages_and_cancel_keeps_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.config import paths
    from jarn.onboarding.tui_wizard import SetupApp

    state_path = tmp_path / "setup-state.json"
    resumed = SetupState(
        stage="confirm",
        answers={
            "provider": "ollama",
            "_provider_group": "local",
            "base_url": "http://localhost:11434",
            "model": "ollama/qwen3",
            "theme": "dark",
        },
        updated_at="2026-08-09T00:00:00Z",
    )
    app = SetupApp(resume_state=resumed, state_path=state_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # Save configuration
        await pilot.pause()

    assert app.result_path == paths.global_config_path()
    assert not paths.global_config_path().exists()
    assert load_setup_state(path=state_path) is not None

    cancel_state = SetupState(stage="provider", answers={}, updated_at="2026-08-09T00:00:00Z")
    cancel = SetupApp(resume_state=cancel_state, state_path=state_path)
    async with cancel.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert cancel._cancelled is True
    assert load_setup_state(path=state_path) is not None


@pytest.mark.asyncio
async def test_transactional_setup_does_not_offer_eager_openrouter_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("jarn.onboarding.tui_wizard._detect_env_key", lambda: None)
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        first = app.query_one("#step-list")
        first.highlighted = 2  # another cloud provider
        await pilot.press("enter")
        await pilot.pause()
        detail = app.query_one("#step-list")
        detail.highlighted = next(
            index for index, option in enumerate(detail._options) if option.id == "opt:openrouter"
        )
        await pilot.press("enter")
        await pilot.pause()
        assert app.step == "storage"
        rendered = "\n".join(str(option.prompt) for option in app.query_one("#step-list")._options)
        assert "browser" not in rendered.lower()
        assert "oauth" not in rendered.lower()
