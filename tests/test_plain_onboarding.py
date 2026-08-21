from __future__ import annotations

from jarn.catalog import CatalogError, CatalogSource, ModelCatalogSnapshot
from jarn.onboarding.state import load_setup_state


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


def test_plain_setup_stages_then_uses_shared_completion_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-opencode-secret")
    _quiet_probes(monkeypatch)
    replies = iter(["opencode", "dark", "save"])
    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", lambda *_a, **_k: next(replies))
    captured: dict[str, object] = {}
    expected = tmp_path / "home" / "config.yaml"

    def finish(answers, *, console, pending_credentials):
        del console
        captured.update(answers)
        assert pending_credentials.get("opencode") is None
        assert not expected.exists()
        return expected

    monkeypatch.setattr("jarn.onboarding.flow.finalize_setup", finish)

    from jarn.onboarding.wizard import run_wizard

    assert run_wizard() == expected
    assert captured["provider"] == "opencode"
    assert captured["key_ref"] == "${OPENCODE_API_KEY}"
    assert captured["theme"] == "dark"
    assert "sk-opencode-secret" not in repr(captured)


def test_plain_cancel_is_non_success_and_keeps_resumable_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _quiet_probes(monkeypatch)
    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", lambda *_a, **_k: "cancel")

    from jarn.onboarding.wizard import run_wizard

    assert run_wizard() is None
    state = load_setup_state()
    assert state is not None
    assert state.stage == "provider"
    assert not (tmp_path / "home" / "config.yaml").exists()


def test_plain_advanced_detail_back_returns_to_simple_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _quiet_probes(monkeypatch)
    replies = iter(["advanced", "back", "cancel"])
    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", lambda *_a, **_k: next(replies))

    from jarn.onboarding.wizard import run_wizard

    assert run_wizard() is None
    state = load_setup_state()
    assert state is not None
    assert state.stage == "provider"


def test_plain_advanced_path_collects_all_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "sk-compatible-secret")
    _quiet_probes(monkeypatch)
    replies = iter(
        [
            "advanced",
            "openai_compatible",
            "https://proxy.example.com",
            "qwen3-coder",
            "high",
            "openai_compatible/qwen3-subagent",
            "anthropic/claude-haiku-4-5",
            "openai_compatible/qwen3-fallback,ollama/qwen3",
            "12.50",
            "70",
            "yes",
            "edit",
            "high-contrast",
            "save",
        ]
    )
    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", lambda *_a, **_k: next(replies))
    captured: dict[str, str] = {}

    def finish(answers, *, console, pending_credentials):
        del console, pending_credentials
        captured.update(answers)
        return tmp_path / "home" / "config.yaml"

    monkeypatch.setattr("jarn.onboarding.flow.finalize_setup", finish)

    from jarn.onboarding.wizard import run_wizard

    assert run_wizard() == tmp_path / "home" / "config.yaml"
    assert captured["model"] == "openai_compatible/qwen3-coder"
    assert captured["reasoning_effort"] == "high"
    assert captured["routing_subagent"] == "openai_compatible/qwen3-subagent"
    assert captured["routing_summarizer"] == "anthropic/claude-haiku-4-5"
    assert captured["routing_fallback"] == ("openai_compatible/qwen3-fallback,ollama/qwen3")
    assert captured["budget_per_session_usd"] == "12.50"
    assert captured["budget_warn_at_pct"] == "70"
    assert captured["budget_hard_stop"] == "true"
    assert captured["permission_mode"] == "auto-edit"
    assert captured["theme"] == "high-contrast"


def test_plain_text_preserves_thai_and_only_slash_commands_navigate(monkeypatch):
    from jarn.onboarding.wizard import _plain_text

    value = "โมเดลภาษาไทย/รุ่นทดลอง"
    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", lambda *_a, **_k: value)
    assert _plain_text("Model") == value
    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", lambda *_a, **_k: "back")
    assert _plain_text("Model") == "back"


def test_plain_declining_existing_config_is_cancel_not_false_success(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    config = tmp_path / "home" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("existing: true\n", encoding="utf-8")
    monkeypatch.setattr("jarn.onboarding.wizard.Confirm.ask", lambda *_a, **_k: False)

    from jarn.onboarding.wizard import run_wizard

    assert run_wizard() is None
    assert config.read_text(encoding="utf-8") == "existing: true\n"
