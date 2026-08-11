from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from jarn.auth import AuthState, AuthStatus, DependencyState, DependencyStatus
from jarn.catalog import (
    CatalogSource,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
    ReasoningEffort,
)
from jarn.codex_dependency import (
    CODEX_OFFICIAL_INSTALL_COMMAND,
    CodexDependencyInstallError,
    CodexInstallPlan,
    CodexInstallResult,
)
from jarn.onboarding.chatgpt import ChatGPTSetupError, prepare_chatgpt_setup


def _auth_status(
    state: AuthState,
    dependency: DependencyState,
    *,
    version: str | None = None,
) -> AuthStatus:
    ready = state is AuthState.AUTHENTICATED_CHATGPT
    return AuthStatus(
        state=state,
        dependency=DependencyStatus(
            state=dependency,
            executable="/old/codex" if version else None,
            version=version,
            minimum_version="0.100.0",
        ),
        checked_at="2026-08-09T00:00:00Z",
        authenticated=ready,
        ready=ready,
        auth_mode="chatgpt" if ready else "signed_out",
        plan_type="plus" if ready else None,
    )


def _plan() -> CodexInstallPlan:
    return CodexInstallPlan(
        version="0.147.0",
        target="x86_64-unknown-linux-musl",
        asset_name="codex-package-x86_64-unknown-linux-musl.tar.gz",
        asset_url="https://releases.openai.com/codex/releases/0.147.0/package.tar.gz",
        asset_sha256="a" * 64,
        checksum_url="https://releases.openai.com/codex/releases/0.147.0/SHA256SUMS",
        checksum_sha256="b" * 64,
        source="OpenAI Releases",
        metadata_url="https://releases.openai.com/codex/channels/latest",
        destination="/home/user/.local/bin/codex",
        release_directory="/home/user/.codex/packages/standalone/releases/0.147.0-linux",
    )


class _Installer:
    def __init__(self, failure: CodexDependencyInstallError | None = None) -> None:
        self.failure = failure
        self.install_called = False

    def resolve_plan(self):
        return _plan()

    def install(self, plan, *, on_progress=None):
        self.install_called = True
        if self.failure:
            raise self.failure
        if on_progress:
            on_progress("verifying")
        return CodexInstallResult(
            plan=plan,
            executable=plan.destination,
            smoke_version=plan.version,
            changed=True,
        )


class _Auth:
    def __init__(self, status: AuthStatus, *, command=None) -> None:
        self._status = status
        self.command = command
        self.cwd = None

    def status(self, *, refresh=False):
        del refresh
        return self._status


class _Catalog:
    def __init__(self) -> None:
        model = ModelCatalogEntry(
            provider_profile="codex_subscription",
            model_id="gpt-account-default",
            ref="codex_subscription/gpt-account-default",
            display_name="GPT Account Default",
            is_default=True,
            account_available=True,
            default_reasoning_effort="medium",
            supported_reasoning_efforts=(
                ReasoningEffort("low"),
                ReasoningEffort("medium"),
                ReasoningEffort("high"),
            ),
        )
        self.snapshot = ModelCatalogSnapshot(
            provider_profile="codex_subscription",
            provider_type="codex_subscription",
            source=CatalogSource.CODEX_LIVE,
            retrieved_at="2026-08-09T00:00:00Z",
            ttl_seconds=3600,
            expires_at="2026-08-09T01:00:00Z",
            stale=False,
            account_fingerprint="account",
            models=(model,),
            availability_verified=True,
            provenance_label="Live Codex account catalog",
        )

    def get_catalog(self, *_args, **_kwargs):
        return self.snapshot

    def validate_selection(self, snapshot, ref, *, reasoning_effort=None):
        assert snapshot is self.snapshot
        assert ref == self.snapshot.models[0].ref
        assert reasoning_effort == "medium"
        return True, ""


@pytest.mark.parametrize(
    ("dependency", "version"),
    [(DependencyState.MISSING, None), (DependencyState.INCOMPATIBLE, "0.1.0")],
)
def test_chatgpt_setup_offers_dependency_before_login_and_decline_is_incomplete(
    dependency, version
):
    initial_state = (
        AuthState.DEPENDENCY_MISSING
        if dependency is DependencyState.MISSING
        else AuthState.DEPENDENCY_INCOMPATIBLE
    )
    installer = _Installer()
    stream = StringIO()

    with pytest.raises(ChatGPTSetupError, match="installation was declined") as caught:
        prepare_chatgpt_setup(
            console=Console(file=stream, width=80),
            auth_service=_Auth(_auth_status(initial_state, dependency, version=version)),
            dependency_installer=installer,
            confirm_install=lambda: False,
        )

    output = stream.getvalue()
    assert installer.install_called is False
    assert "Purpose:" in output
    assert "Version/channel:" in output
    assert "Source:" in output
    assert "Destination:" in output
    assert CODEX_OFFICIAL_INSTALL_COMMAND in str(caught.value)
    assert "Verified ChatGPT" not in output


def test_chatgpt_setup_install_failure_cannot_report_success():
    installer = _Installer(CodexDependencyInstallError("checksum", "digest mismatch"))
    stream = StringIO()

    with pytest.raises(ChatGPTSetupError) as caught:
        prepare_chatgpt_setup(
            console=Console(file=stream, width=80),
            auth_service=_Auth(_auth_status(AuthState.DEPENDENCY_MISSING, DependencyState.MISSING)),
            dependency_installer=installer,
            confirm_install=lambda: True,
        )

    assert CODEX_OFFICIAL_INSTALL_COMMAND in str(caught.value)
    assert "Verified ChatGPT" not in stream.getvalue()


def test_chatgpt_setup_installs_then_requires_verified_account(monkeypatch):
    installer = _Installer()
    signed_out = _auth_status(AuthState.SIGNED_OUT, DependencyState.COMPATIBLE, version="0.147.0")
    monkeypatch.setattr(
        "jarn.onboarding.chatgpt.CodexAuthService",
        lambda *, command: _Auth(signed_out, command=command),
    )
    stream = StringIO()

    with pytest.raises(ChatGPTSetupError, match="sign-in is required"):
        prepare_chatgpt_setup(
            console=Console(file=stream, width=80),
            auth_service=_Auth(_auth_status(AuthState.DEPENDENCY_MISSING, DependencyState.MISSING)),
            dependency_installer=installer,
            confirm_install=lambda: True,
            confirm_login=lambda: False,
        )

    assert installer.install_called is True
    assert "Verified ChatGPT" not in stream.getvalue()


def test_chatgpt_setup_installs_verifies_account_and_uses_live_default(monkeypatch):
    installer = _Installer()
    ready = _auth_status(
        AuthState.AUTHENTICATED_CHATGPT,
        DependencyState.COMPATIBLE,
        version="0.147.0",
    )
    monkeypatch.setattr(
        "jarn.onboarding.chatgpt.CodexAuthService",
        lambda *, command: _Auth(ready, command=command),
    )
    stream = StringIO()

    result = prepare_chatgpt_setup(
        console=Console(file=stream, width=80),
        auth_service=_Auth(_auth_status(AuthState.DEPENDENCY_MISSING, DependencyState.MISSING)),
        dependency_installer=installer,
        catalog_service=_Catalog(),
        confirm_install=lambda: True,
    )

    assert result.auth.ready is True
    assert result.model.model_id == "gpt-account-default"
    assert result.reasoning_effort == "medium"
    assert "Verified Codex CLI 0.147.0" in stream.getvalue()
    assert "Verified ChatGPT" in stream.getvalue()


def test_setup_and_model_picker_render_the_identical_catalog_snapshot(tmp_path):
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
    from jarn.tui.controller import Controller

    catalog = _Catalog()
    ready = _auth_status(
        AuthState.AUTHENTICATED_CHATGPT,
        DependencyState.COMPATIBLE,
        version="0.147.0",
    )
    setup = prepare_chatgpt_setup(
        console=Console(file=StringIO()),
        auth_service=_Auth(ready, command="/bin/codex"),
        catalog_service=catalog,
    )
    config = Config(
        default_profile="codex_subscription",
        default_model=setup.model.ref,
        providers={
            "codex_subscription": ProviderConfig(type=ProviderType.CODEX_SUBSCRIPTION)
        },
        routing=RoutingConfig(main=setup.model.ref),
    )
    controller = Controller(config, tmp_path)
    controller._model_catalog_snapshot = setup.catalog

    choices = dict(controller.model_choices())

    assert set(choices) == {entry.ref for entry in setup.catalog.visible_models()}
    assert setup.model.ref in choices
    assert setup.catalog.provenance_label in choices[setup.model.ref]
    controller.close()
