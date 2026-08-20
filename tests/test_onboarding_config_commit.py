from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from io import StringIO

import pytest
import yaml
from rich.console import Console

from jarn.install_state import InstallRecord
from jarn.onboarding.completion import (
    InstallIdentity,
    SetupCompletionError,
    verify_install_identity,
)
from jarn.onboarding.config_commit import (
    SetupConfigError,
    commit_staged_config,
    rollback_setup_commit,
    stage_setup_config,
)
from jarn.onboarding.credentials import (
    ActivatedCredential,
    CredentialActivationError,
    PendingCredentials,
    activate_pending_credential,
    rollback_activated_credential,
)
from jarn.onboarding.flow import SetupFlowError, finalize_setup
from jarn.tui.i18n import resolve_locale, t

_EXISTING = """\
# keep this operator comment
config_version: 3
default_profile: custom-team
default_model: custom-team/original
permission_mode: auto-edit
providers:
  custom-team:
    type: openai_compatible
    api_key: ${TEAM_KEY}
    base_url: https://gateway.example/v1
    headers:
      X-Tenant: alpha
    timeout: 17
    extra:
      vendor_flag: keep-me
  anthropic:
    type: anthropic
    api_key: ${OLD_ANTHROPIC_KEY}
    headers:
      X-Debug: keep
    max_retries: 9
routing:
  main: custom-team/original
  subagent: custom-team/special-subagent
  summarizer: custom-team/special-summarizer
  fallback:
    - custom-team/fallback
  prompt_cache: "off"
  keep_alive: 99
permissions:
  allow: ["git status"]
  deny: ["rm -rf"]
ui:
  theme: dark
  accent: magenta
"""


@pytest.mark.skipif(os.name == "nt", reason="J.A.R.N. supports Windows through WSL2")
def test_completion_identity_smokes_the_user_visible_unmanaged_command(tmp_path, monkeypatch):
    from jarn.version import __version__

    executable = tmp_path / "jarn"
    executable.write_text(f"#!/bin/sh\necho 'J.A.R.N. {__version__}'\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "no-managed-state"))
    monkeypatch.setattr("jarn.onboarding.completion.shutil.which", lambda _name: str(executable))

    identity = verify_install_identity()

    assert identity == InstallIdentity(
        executable=str(executable.absolute()),
        version=__version__,
        method="unmanaged/Python package",
    )


@pytest.mark.skipif(os.name == "nt", reason="J.A.R.N. supports Windows through WSL2")
def test_completion_refuses_unmanaged_entrypoint_shadowed_on_path(tmp_path, monkeypatch):
    from jarn.version import __version__

    invoked = tmp_path / "venv" / "bin" / "jarn"
    shadow = tmp_path / "legacy" / "jarn"
    for executable in (invoked, shadow):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(f"#!/bin/sh\necho 'jarn {__version__}'\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "no-managed-state"))
    monkeypatch.setattr("sys.argv", [str(invoked), "setup"])
    monkeypatch.setattr("jarn.onboarding.completion.shutil.which", lambda _name: str(shadow))

    with pytest.raises(SetupCompletionError, match="setup was launched from"):
        verify_install_identity()


@pytest.mark.skipif(os.name == "nt", reason="J.A.R.N. supports Windows through WSL2")
def test_completion_refuses_unmanaged_command_from_another_version(tmp_path, monkeypatch):
    shadow = tmp_path / "legacy" / "jarn"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("#!/bin/sh\necho 'jarn 0.4.4'\n", encoding="utf-8")
    shadow.chmod(0o755)
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "no-managed-state"))
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr("jarn.onboarding.completion.shutil.which", lambda _name: str(shadow))

    with pytest.raises(SetupCompletionError, match="ordinary `jarn` resolves.*reports 0.4.4"):
        verify_install_identity()


def test_completion_identity_does_not_ignore_unsafe_managed_record(tmp_path, monkeypatch):
    manifest = tmp_path / "install.json"
    manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("jarn.onboarding.completion.shutil.which", lambda _name: "/bin/jarn")

    with pytest.raises(SetupCompletionError, match="not safe/actionable"):
        verify_install_identity(manifest_path=manifest)


@pytest.mark.skipif(os.name == "nt", reason="J.A.R.N. supports Windows through WSL2")
def test_completion_refuses_healthy_managed_binary_shadowed_on_path(tmp_path, monkeypatch):
    state = tmp_path / "state" / "jarn"
    active = tmp_path / "bin" / "jarn"
    shadow = tmp_path / "legacy" / "jarn"
    state.mkdir(parents=True)
    active.parent.mkdir(parents=True)
    shadow.parent.mkdir(parents=True)
    for executable in (active, shadow):
        executable.write_text("#!/bin/sh\necho 'jarn 0.11.0'\n", encoding="utf-8")
        executable.chmod(0o755)
    manifest = state / "install.json"
    manifest.write_text("{}\n", encoding="utf-8")
    record = InstallRecord(
        schema_version=1,
        version="0.11.0",
        method="python",
        channel="stable",
        active_path=active,
        state_dir=state,
    )
    monkeypatch.setattr(
        "jarn.onboarding.completion.load_actionable_install_record", lambda _path: record
    )
    monkeypatch.setattr("jarn.onboarding.completion.shutil.which", lambda _name: str(shadow))

    with pytest.raises(SetupCompletionError, match="resolves to.*legacy.*instead of"):
        verify_install_identity(manifest_path=manifest)


def test_rerun_deep_merges_and_creates_exact_timestamped_backup(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_EXISTING, encoding="utf-8")
    source_mode = 0o600 if os.name == "nt" else 0o400
    path.chmod(source_mode)

    staged = stage_setup_config(
        path,
        provider="anthropic",
        api_key_ref="${ANTHROPIC_API_KEY}",
        model="anthropic/claude-sonnet-4-5",
        theme="light",
    )

    # Staging is a pure operation.
    assert path.read_text(encoding="utf-8") == _EXISTING
    result = commit_staged_config(staged, now=datetime(2026, 8, 9, 1, 2, 3, 456789, tzinfo=UTC))

    assert result.backup_path is not None
    assert result.backup_path.name == "config.yaml.bak.20260809T010203.456789Z"
    assert result.backup_path.read_text(encoding="utf-8") == _EXISTING
    if os.name == "nt":
        # Native Windows has no POSIX owner-only mode; Python maps a writable
        # file to the platform's 0666-style permission bits.
        assert os.stat(path).st_mode & 0o222
    else:
        assert os.stat(path).st_mode & 0o777 == source_mode
        assert os.stat(result.backup_path).st_mode & 0o777 == source_mode
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["config_version"] == 3
    assert raw["default_profile"] == "anthropic"
    assert raw["default_model"] == "anthropic/claude-sonnet-4-5"
    assert raw["permission_mode"] == "auto-edit"
    assert raw["providers"]["custom-team"]["headers"] == {"X-Tenant": "alpha"}
    assert raw["providers"]["custom-team"]["timeout"] == 17
    assert raw["providers"]["custom-team"]["extra"] == {"vendor_flag": "keep-me"}
    assert raw["providers"]["anthropic"]["headers"] == {"X-Debug": "keep"}
    assert raw["providers"]["anthropic"]["max_retries"] == 9
    assert raw["providers"]["anthropic"]["api_key"] == "${ANTHROPIC_API_KEY}"
    assert raw["routing"] == {
        "main": "anthropic/claude-sonnet-4-5",
        "subagent": "custom-team/special-subagent",
        "summarizer": "custom-team/special-summarizer",
        "fallback": ["custom-team/fallback"],
        "prompt_cache": "off",
        "keep_alive": 99,
    }
    assert raw["permissions"] == {"allow": ["git status"], "deny": ["rm -rf"]}
    assert raw["ui"] == {"theme": "light", "accent": "magenta"}
    assert "# keep this operator comment" in path.read_text(encoding="utf-8")


def test_advanced_setup_overrides_explicit_controls_and_preserves_unrelated_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_EXISTING, encoding="utf-8")

    staged = stage_setup_config(
        path,
        provider="anthropic",
        api_key_ref="${ANTHROPIC_API_KEY}",
        model="anthropic/claude-opus-4-8",
        theme="high-contrast",
        mode="plan",
        reasoning_effort="high",
        routing_subagent="anthropic/claude-sonnet-4-5",
        routing_summarizer="custom-team/summary",
        routing_fallback=["ollama/qwen3", "openrouter/openai/gpt-5.4"],
        budget_per_session_usd=12.5,
        budget_hard_stop=False,
        budget_warn_at_pct=70,
    )

    raw = staged.candidate
    assert raw["permission_mode"] == "plan"
    assert raw["providers"]["anthropic"]["reasoning_effort"] == "high"
    assert raw["routing"]["main"] == "anthropic/claude-opus-4-8"
    assert raw["routing"]["subagent"] == "anthropic/claude-sonnet-4-5"
    assert raw["routing"]["summarizer"] == "custom-team/summary"
    assert raw["routing"]["fallback"] == [
        "ollama/qwen3",
        "openrouter/openai/gpt-5.4",
    ]
    assert raw["routing"]["prompt_cache"] == "off"
    assert raw["budget"] == {
        "per_session_usd": 12.5,
        "hard_stop": False,
        "warn_at_pct": 70,
    }
    assert raw["permissions"] == {"allow": ["git status"], "deny": ["rm -rf"]}
    assert raw["providers"]["custom-team"]["headers"] == {"X-Tenant": "alpha"}


def test_concurrent_edit_is_not_overwritten(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_EXISTING, encoding="utf-8")
    staged = stage_setup_config(
        path,
        provider="anthropic",
        api_key_ref="${ANTHROPIC_API_KEY}",
        model="anthropic/claude-sonnet-4-5",
        theme="dark",
    )
    newer = _EXISTING + "# edited while setup was open\n"
    path.write_text(newer, encoding="utf-8")

    with pytest.raises(SetupConfigError, match="changed while setup was open"):
        commit_staged_config(staged)

    assert path.read_text(encoding="utf-8") == newer
    assert not list(tmp_path.glob("config.yaml.bak.*"))


def test_explicit_rollback_restores_previous_bytes(tmp_path):
    path = tmp_path / "config.yaml"
    original = _EXISTING.replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(original)
    staged = stage_setup_config(
        path,
        provider="ollama",
        api_key_ref=None,
        model="ollama/qwen3",
        theme="high-contrast",
        base_url="http://localhost:11434",
    )
    committed = commit_staged_config(staged)
    assert path.read_bytes() != original

    rollback_setup_commit(committed)

    assert path.read_bytes() == original


def test_finalize_discloses_billable_validation_and_clears_state_only_on_success(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    config = tmp_path / "config.yaml"
    state = tmp_path / "setup-state.json"
    stream = StringIO()
    questions: list[str] = []
    monkeypatch.setattr(
        "jarn.onboarding.flow.verify_install_identity",
        lambda: InstallIdentity("/home/user/.local/bin/jarn", "0.11.0", "portable-python"),
    )
    monkeypatch.setattr(
        "jarn.onboarding.flow.Confirm.ask",
        lambda question, **_kwargs: questions.append(question) or True,
    )
    monkeypatch.setattr("jarn.onboarding.wizard.validate_config", lambda *_a, **_k: True)

    result = finalize_setup(
        {
            "provider": "anthropic",
            "key_ref": "${ANTHROPIC_API_KEY}",
            "model": "anthropic/claude-sonnet-4-5",
            "theme": "dark",
        },
        console=Console(file=stream, width=100),
        state_path=state,
        config_path=config,
    )

    assert result == config
    assert not state.exists()
    committed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert committed["routing"]["main"] == "anthropic/claude-sonnet-4-5"
    assert committed["routing"]["subagent"] == "anthropic/claude-sonnet-4-5"
    assert committed["routing"]["summarizer"] == "anthropic/claude-sonnet-4-5"
    loc = resolve_locale()
    assert questions == [t("onboarding.validate.confirm", loc)]
    output = stream.getvalue()
    plain = " ".join(output.split())
    assert t("onboarding.validate.required", loc) in plain
    assert t("onboarding.validate.credits", loc) in plain
    assert t("onboarding.complete.banner", loc) in plain
    assert "/home/user/.local/bin/jarn" in output
    assert "portable-python" in output
    assert t("onboarding.complete.auth.api", loc) in plain
    assert "Ask before changes" in output
    assert t("onboarding.complete.cwd", loc) in plain
    assert t("onboarding.complete.next", loc) in plain
    assert "jarn" in output


def test_failed_validation_keeps_resume_state_and_does_not_commit(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    state = tmp_path / "setup-state.json"
    monkeypatch.setattr("jarn.onboarding.flow.Confirm.ask", lambda *_a, **_k: True)
    monkeypatch.setattr("jarn.onboarding.wizard.validate_config", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "jarn.onboarding.flow.verify_install_identity",
        lambda: InstallIdentity("/bin/jarn", "0.11.0", "test"),
    )

    with pytest.raises(SetupFlowError, match="validation did not succeed"):
        finalize_setup(
            {
                "provider": "anthropic",
                "key_ref": "${ANTHROPIC_API_KEY}",
                "model": "anthropic/claude-sonnet-4-5",
                "theme": "dark",
            },
            console=Console(file=StringIO()),
            state_path=state,
            config_path=config,
        )

    assert not config.exists()
    assert json.loads(state.read_text(encoding="utf-8"))["stage"] == "confirm"


def test_declined_billable_validation_sends_nothing_and_cannot_report_complete(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.yaml"
    state = tmp_path / "setup-state.json"
    calls: list[str] = []
    monkeypatch.setattr("jarn.onboarding.flow.Confirm.ask", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "jarn.onboarding.wizard.validate_config",
        lambda *_a, **_k: calls.append("sent") or True,
    )
    stream = StringIO()

    with pytest.raises(SetupFlowError, match="declined"):
        finalize_setup(
            {
                "provider": "anthropic",
                "key_ref": "${ANTHROPIC_API_KEY}",
                "model": "anthropic/claude-sonnet-4-5",
                "theme": "dark",
            },
            console=Console(file=stream),
            state_path=state,
            config_path=config,
        )

    assert calls == []
    assert not config.exists()
    assert "Setup complete" not in stream.getvalue()


def test_unverified_executable_keeps_resume_state_and_does_not_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    config = tmp_path / "config.yaml"
    state = tmp_path / "setup-state.json"
    monkeypatch.setattr("jarn.onboarding.flow.Confirm.ask", lambda *_a, **_k: True)
    monkeypatch.setattr("jarn.onboarding.wizard.validate_config", lambda *_a, **_k: True)

    def fail_identity():
        raise SetupCompletionError("active executable is shadowed")

    monkeypatch.setattr("jarn.onboarding.flow.verify_install_identity", fail_identity)

    with pytest.raises(SetupFlowError, match="shadowed"):
        finalize_setup(
            {
                "provider": "anthropic",
                "key_ref": "${ANTHROPIC_API_KEY}",
                "model": "anthropic/claude-sonnet-4-5",
                "theme": "dark",
            },
            console=Console(file=StringIO()),
            state_path=state,
            config_path=config,
        )

    assert not config.exists()
    assert state.exists()


def test_unreachable_local_model_is_incomplete_and_uncommitted(tmp_path, monkeypatch):
    from jarn.catalog import CatalogError, CatalogSource, ModelCatalogSnapshot

    config = tmp_path / "config.yaml"
    state = tmp_path / "setup-state.json"
    snapshot = ModelCatalogSnapshot(
        provider_profile="ollama",
        provider_type="ollama",
        source=CatalogSource.STATIC_FALLBACK,
        retrieved_at="2026-08-09T00:00:00Z",
        ttl_seconds=3600,
        expires_at="2026-08-09T01:00:00Z",
        stale=False,
        account_fingerprint=None,
        models=(),
        availability_verified=False,
        provenance_label="Static fallback (availability unverified)",
        error=CatalogError("MODEL_CATALOG_UNAVAILABLE", "connection refused"),
    )

    class Catalog:
        def get_catalog(self, *_args, **_kwargs):
            return snapshot

    monkeypatch.setattr("jarn.onboarding.flow.ModelCatalogService", Catalog)

    with pytest.raises(SetupFlowError, match="did not report the selected model"):
        finalize_setup(
            {
                "provider": "ollama",
                "base_url": "http://localhost:11434",
                "model": "ollama/qwen3",
                "theme": "dark",
            },
            console=Console(file=StringIO()),
            state_path=state,
            config_path=config,
        )

    assert not config.exists()
    assert state.exists()


def test_pending_credential_uses_unique_file_and_rollback_removes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    def no_keychain(*_args, **_kwargs):
        raise RuntimeError("headless")

    monkeypatch.setattr("jarn.config.secrets._keyring_call", no_keychain)
    pending = PendingCredentials()
    pending.set("anthropic", "sk-ant-memory-only")
    assert "sk-ant-memory-only" not in repr(pending)

    active = activate_pending_credential("anthropic", pending.get("anthropic") or "", token="fixed")
    secret_path = tmp_path / "home" / "secrets" / "jarn" / "anthropic.setup-fixed"
    assert active.reference == "file:jarn/anthropic.setup-fixed"
    assert secret_path.read_text(encoding="utf-8") == "sk-ant-memory-only"

    rollback_activated_credential(active)

    assert not secret_path.exists()


def test_keychain_rollback_uses_bounded_isolated_delete(monkeypatch):
    from jarn.config.secrets import StoredSecret

    deleted: list[tuple[str, str, float]] = []
    monkeypatch.setattr(
        "jarn.onboarding.credentials.delete_keychain_secret",
        lambda service, account, *, timeout: deleted.append((service, account, timeout)),
    )
    active = ActivatedCredential(
        provider="anthropic",
        service="jarn",
        account="anthropic.setup-owned",
        stored=StoredSecret(
            reference="keychain:jarn/anthropic.setup-owned",
            backend="keychain",
        ),
    )

    rollback_activated_credential(active)

    assert deleted == [("jarn", "anthropic.setup-owned", 5.0)]


def test_keychain_rollback_timeout_is_controlled(monkeypatch):
    from jarn.config.secrets import StoredSecret

    def _timeout(*_args, **_kwargs):
        raise TimeoutError("blocked backend")

    monkeypatch.setattr("jarn.onboarding.credentials.delete_keychain_secret", _timeout)
    active = ActivatedCredential(
        provider="anthropic",
        service="jarn",
        account="anthropic.setup-owned",
        stored=StoredSecret(
            reference="keychain:jarn/anthropic.setup-owned",
            backend="keychain",
        ),
    )

    with pytest.raises(CredentialActivationError, match="isolated worker was terminated"):
        rollback_activated_credential(active)


def test_commit_failure_discards_new_credential_and_preserves_old_config(tmp_path, monkeypatch):
    from jarn.config.secrets import StoredSecret

    config = tmp_path / "config.yaml"
    config.write_text(_EXISTING, encoding="utf-8")
    state = tmp_path / "setup-state.json"
    pending = PendingCredentials()
    pending.set("anthropic", "sk-ant-new-secret")
    answers = {
        "provider": "anthropic",
        "_credential_pending": "memory",
        "model": "anthropic/claude-sonnet-4-5",
        "theme": "dark",
    }
    activated = ActivatedCredential(
        provider="anthropic",
        service="jarn",
        account="anthropic.setup-new",
        stored=StoredSecret(reference="keychain:jarn/anthropic.setup-new", backend="keychain"),
    )
    rolled_back: list[ActivatedCredential] = []
    monkeypatch.setattr("jarn.onboarding.flow.Confirm.ask", lambda *_a, **_k: True)
    monkeypatch.setattr("jarn.onboarding.wizard.validate_config", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "jarn.onboarding.flow.verify_install_identity",
        lambda: InstallIdentity("/bin/jarn", "0.11.0", "test"),
    )
    monkeypatch.setattr(
        "jarn.onboarding.flow.activate_pending_credential", lambda *_a, **_k: activated
    )
    monkeypatch.setattr("jarn.onboarding.flow.rollback_activated_credential", rolled_back.append)
    # This test isolates the later transactional commit/credential rollback.
    # Cross-provider route readiness has dedicated tests below.
    monkeypatch.setattr("jarn.onboarding.flow._validate_staged_routes", lambda *_a: None)
    monkeypatch.setattr(
        "jarn.onboarding.flow.commit_staged_config",
        lambda *_a, **_k: (_ for _ in ()).throw(SetupConfigError("concurrent edit")),
    )

    with pytest.raises(SetupFlowError, match="concurrent edit"):
        finalize_setup(
            answers,
            console=Console(file=StringIO()),
            state_path=state,
            config_path=config,
            pending_credentials=pending,
        )

    assert config.read_text(encoding="utf-8") == _EXISTING
    assert rolled_back == [activated]
    assert json.loads(state.read_text(encoding="utf-8"))["stage"] == "key"
    assert "sk-ant-new-secret" not in state.read_text(encoding="utf-8")
    assert not any(name.startswith("JARN_SETUP_CREDENTIAL_") for name in os.environ)


def test_completion_record_failure_rolls_back_published_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    config = tmp_path / "config.yaml"
    config.write_text(_EXISTING, encoding="utf-8")
    state = tmp_path / "setup-state.json"
    monkeypatch.setattr("jarn.onboarding.flow.Confirm.ask", lambda *_a, **_k: True)
    monkeypatch.setattr("jarn.onboarding.wizard.validate_config", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "jarn.onboarding.flow.verify_install_identity",
        lambda: InstallIdentity("/bin/jarn", "0.11.0", "test"),
    )
    monkeypatch.setattr(
        "jarn.onboarding.flow.set_setup_progress",
        lambda _status: (_ for _ in ()).throw(SetupFlowError("manifest is read-only")),
    )
    # This test isolates post-commit completion rollback; route readiness is
    # exercised independently.
    monkeypatch.setattr("jarn.onboarding.flow._validate_staged_routes", lambda *_a: None)

    with pytest.raises(SetupFlowError, match="previous config was restored"):
        finalize_setup(
            {
                "provider": "anthropic",
                "key_ref": "${ANTHROPIC_API_KEY}",
                "model": "anthropic/claude-sonnet-4-5",
                "theme": "light",
            },
            console=Console(file=StringIO()),
            state_path=state,
            config_path=config,
        )

    assert config.read_text(encoding="utf-8") == _EXISTING
    assert state.exists()


def _cross_provider_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    staged = stage_setup_config(
        tmp_path / "candidate.yaml",
        provider="openai",
        api_key_ref="${OPENAI_API_KEY}",
        model="openai/gpt-main",
        theme="dark",
        routing_subagent="anthropic/claude-worker",
        routing_summarizer="openai/gpt-main",
        routing_fallback=["anthropic/claude-fallback"],
    )
    staged.candidate["providers"]["anthropic"] = {
        "type": "anthropic",
        "api_key": "${ANTHROPIC_API_KEY}",
    }
    return staged.candidate


def test_staged_route_gate_accepts_every_verified_cross_provider_route(tmp_path, monkeypatch):
    from jarn.catalog import ModelCatalogCache, ModelCatalogService
    from jarn.onboarding.flow import _validate_staged_routes
    from jarn.providers import RemoteModelCatalog, RemoteModelRecord

    def remote(provider, **_kwargs):
        models = (
            (RemoteModelRecord("gpt-main"),)
            if provider.type.value == "openai"
            else (
                RemoteModelRecord("claude-worker"),
                RemoteModelRecord("claude-fallback"),
            )
        )
        return RemoteModelCatalog(models, f"live {provider.type.value}", provider.type.value)

    monkeypatch.setattr("jarn.catalog.service.fetch_remote_model_catalog", remote)
    service = ModelCatalogService(cache=ModelCatalogCache(tmp_path / "catalog-cache"))

    _validate_staged_routes(_cross_provider_candidate(tmp_path, monkeypatch), service)


def test_staged_route_gate_classifies_missing_background_credential_as_auth(tmp_path, monkeypatch):
    from jarn.catalog import ModelCatalogCache, ModelCatalogService
    from jarn.onboarding.flow import _validate_staged_routes
    from jarn.onboarding.outcome import SetupFailureKind
    from jarn.providers import (
        RemoteModelCatalog,
        RemoteModelDiscoveryError,
        RemoteModelRecord,
    )

    candidate = _cross_provider_candidate(tmp_path, monkeypatch)
    candidate["providers"]["anthropic"]["api_key"] = "${MISSING_ROUTE_KEY}"
    monkeypatch.delenv("MISSING_ROUTE_KEY", raising=False)

    def remote(provider, **_kwargs):
        if provider.type.value == "anthropic":
            raise RemoteModelDiscoveryError("anthropic model-list credential could not be resolved")
        return RemoteModelCatalog(
            (RemoteModelRecord("gpt-main"),),
            "live openai",
            "openai-scope",
        )

    monkeypatch.setattr("jarn.catalog.service.fetch_remote_model_catalog", remote)
    service = ModelCatalogService(cache=ModelCatalogCache(tmp_path / "catalog-cache"))

    with pytest.raises(SetupFlowError, match="subagent") as caught:
        _validate_staged_routes(candidate, service)

    assert caught.value.kind is SetupFailureKind.AUTH
    assert "credential" in str(caught.value).lower()


def test_staged_route_gate_rejects_stale_exact_validation_evidence(tmp_path, monkeypatch):
    from jarn.catalog import ModelCatalogCache, ModelCatalogService
    from jarn.config.schema import ProviderConfig, ProviderType
    from jarn.onboarding.flow import _validate_staged_routes
    from jarn.onboarding.outcome import SetupFailureKind
    from jarn.providers import RemoteModelCatalog, RemoteModelRecord

    now = [datetime(2026, 8, 9, 12, tzinfo=UTC)]
    cache = ModelCatalogCache(tmp_path / "catalog-cache", clock=lambda: now[0].timestamp())
    service = ModelCatalogService(cache=cache, clock=lambda: now[0], ttl_seconds=60)
    deepseek = ProviderConfig(type=ProviderType.DEEPSEEK, api_key="deepseek-key")
    service.record_billable_validation("deepseek", deepseek, "deepseek/validated-worker")
    now[0] = datetime(2026, 8, 9, 14, tzinfo=UTC)
    candidate = _cross_provider_candidate(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    candidate["providers"]["deepseek"] = {
        "type": "deepseek",
        "api_key": "${DEEPSEEK_API_KEY}",
    }
    candidate["routing"]["subagent"] = "deepseek/validated-worker"
    candidate["routing"]["fallback"] = []
    monkeypatch.setattr(
        "jarn.catalog.service.fetch_remote_model_catalog",
        lambda _provider, **_kwargs: RemoteModelCatalog(
            (RemoteModelRecord("gpt-main"),),
            "live openai",
            "openai-scope",
        ),
    )

    with pytest.raises(SetupFlowError, match="validated-worker") as caught:
        _validate_staged_routes(candidate, service)

    assert caught.value.kind is SetupFailureKind.MODEL
    assert "availability could not be verified" in str(caught.value)
