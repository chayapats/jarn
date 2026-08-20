"""S11 — onboarding wizard chrome through ``t()`` in both locales."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from jarn.catalog import CatalogError, CatalogSource, ModelCatalogSnapshot
from jarn.onboarding.completion import (
    InstallIdentity,
    SetupCompletion,
    render_setup_completion,
)
from jarn.onboarding.state import load_setup_state
from jarn.tui.i18n import t

_LOCALE_ENV = (
    ("en", "en_US.UTF-8"),
    ("th", "th_TH.UTF-8"),
)


def _force_locale(monkeypatch, lang: str) -> None:
    monkeypatch.setenv("LC_ALL", lang)
    monkeypatch.setenv("LANG", lang)
    monkeypatch.delenv("LC_MESSAGES", raising=False)


def _quiet_probes(monkeypatch) -> None:
    monkeypatch.setattr("jarn.onboarding.wizard._chatgpt_session_ready", lambda: False)
    monkeypatch.setattr("jarn.onboarding.wizard._detect_local_providers", lambda: ())

    def unavailable(answers, _pending):
        provider = answers["provider"]
        return ModelCatalogSnapshot(
            provider_profile=provider,
            provider_type=provider,
            source=CatalogSource.STATIC_FALLBACK,
            retrieved_at="2026-08-09T00:00:00Z",
            ttl_seconds=3600,
            expires_at="2026-08-09T01:00:00Z",
            stale=False,
            account_fingerprint=None,
            models=(),
            availability_verified=False,
            provenance_label="Offline fallback; availability unverified",
            error=CatalogError("MODEL_CATALOG_UNAVAILABLE", "fixture discovery disabled"),
        )

    monkeypatch.setattr("jarn.onboarding.wizard._catalog_for_answers", unavailable)


def _force_locale(monkeypatch, lang: str) -> None:
    monkeypatch.setenv("LC_ALL", lang)
    monkeypatch.setenv("LANG", lang)
    monkeypatch.delenv("LC_MESSAGES", raising=False)


@pytest.mark.parametrize(("locale", "lang"), _LOCALE_ENV)
def test_plain_wizard_connect_prompt_follows_locale(tmp_path, monkeypatch, locale, lang):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _force_locale(monkeypatch, lang)
    _quiet_probes(monkeypatch)
    prompts: list[str] = []

    def ask(prompt, **_kwargs):
        prompts.append(str(prompt))
        return "cancel"

    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", ask)

    from jarn.onboarding.wizard import run_wizard

    assert run_wizard() is None
    assert t("onboarding.connect.prompt", locale) in prompts
    assert t("onboarding.connect.prompt", "en") != t("onboarding.connect.prompt", "th")
    state = load_setup_state()
    assert state is not None
    assert state.stage == "provider"


@pytest.mark.parametrize(("locale", "lang"), _LOCALE_ENV)
def test_plain_wizard_intro_is_localized(tmp_path, monkeypatch, locale, lang, capsys):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _force_locale(monkeypatch, lang)
    _quiet_probes(monkeypatch)
    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", lambda *_a, **_k: "cancel")

    from jarn.onboarding.wizard import run_wizard

    run_wizard()
    out = capsys.readouterr().out
    if locale == "en":
        assert "Let's get you set up" in out
    else:
        assert "มาตั้งค่ากัน" in out
    assert "~/.jarn/config.yaml" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(("locale", "lang"), _LOCALE_ENV)
async def test_tui_connect_title_follows_locale(tmp_path, monkeypatch, locale, lang):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _force_locale(monkeypatch, lang)
    from jarn.onboarding.tui_wizard import SetupApp

    app = SetupApp()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        title = str(app.query_one("#title").render())
        assert title == t("onboarding.connect.prompt", locale)
        help_line = str(app.query_one("#help").render())
        assert help_line == t("onboarding.tui.help", locale)
        assert app.locale == locale


@pytest.mark.parametrize(("locale", "lang"), _LOCALE_ENV)
def test_completion_next_step_follows_locale(monkeypatch, locale, lang):
    _force_locale(monkeypatch, lang)
    stream = StringIO()
    summary = SetupCompletion(
        install=InstallIdentity("/bin/jarn", "1.0.0", "test"),
        config_path=Path("/tmp/config.yaml"),
        backup_path=None,
        provider="anthropic",
        model="anthropic/claude-sonnet-4-5",
        model_display="anthropic/claude-sonnet-4-5",
        reasoning_effort=None,
        permission_mode="ask",
        cwd=Path("/tmp"),
        auth_mode="API key reference",
        validation="not required",
    )
    render_setup_completion(Console(file=stream, width=100, highlight=False), summary)
    out = stream.getvalue()
    assert t("onboarding.complete.banner", locale) in out
    assert t("onboarding.complete.next", locale) in out
    assert "anthropic" in out
    assert "jarn" in out
    other = "th" if locale == "en" else "en"
    assert t("onboarding.complete.banner", other) not in out


def test_en_and_th_onboarding_prompts_differ():
    assert t("onboarding.connect.prompt", "th") == "ต้องการเชื่อมต่อแบบไหน?"
    assert t("onboarding.connect.prompt", "en") == "How do you want to connect?"
    assert t("onboarding.validate.confirm", "th") != t("onboarding.validate.confirm", "en")
    assert t("onboarding.complete.banner", "th") == "ตั้งค่าเสร็จแล้ว"
    assert "anthropic" not in t("onboarding.connect.prompt", "th")
