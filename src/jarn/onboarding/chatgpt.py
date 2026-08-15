"""Verified ChatGPT setup shared by plain and Textual onboarding."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console
from rich.prompt import Confirm

from jarn.auth import (
    CODEX_OFFICIAL_INSTALL_COMMAND,
    AuthServiceError,
    AuthState,
    AuthStatus,
    CodexAuthService,
    CodexDependencyInstaller,
    CodexDependencyInstallError,
    DependencyState,
    LoginMethod,
    detect_login_method,
    login_interactive,
)
from jarn.catalog import ModelCatalogEntry, ModelCatalogService, ModelCatalogSnapshot
from jarn.config.schema import ProviderConfig, ProviderType
from jarn.config.secrets import redact_secrets
from jarn.onboarding.outcome import SetupCommandError, SetupFailureKind
from jarn.tui import grammar, layout


class ChatGPTSetupError(SetupCommandError):
    """Setup could not prove auth and model readiness, so config must not commit."""


def _auth_failure_kind(status: AuthStatus) -> SetupFailureKind:
    if status.state in (AuthState.DEPENDENCY_MISSING, AuthState.DEPENDENCY_INCOMPATIBLE):
        return SetupFailureKind.DEPENDENCY
    if status.state is AuthState.NETWORK_UNAVAILABLE:
        return SetupFailureKind.NETWORK
    return SetupFailureKind.AUTH


@dataclass(frozen=True, slots=True)
class ChatGPTSetupResult:
    auth: AuthStatus
    catalog: ModelCatalogSnapshot
    model: ModelCatalogEntry
    reasoning_effort: str | None


def prepare_chatgpt_setup(
    *,
    console: Console,
    auth_service: CodexAuthService | None = None,
    catalog_service: ModelCatalogService | None = None,
    dependency_installer: CodexDependencyInstaller | None = None,
    confirm_install: Callable[[], bool] | None = None,
    confirm_login: Callable[[], bool] | None = None,
    login_method: LoginMethod | None = None,
) -> ChatGPTSetupResult:
    """Verify ChatGPT, fetch the account catalog, and choose its live default.

    No configuration is written here.  Callers stage their answers, call this
    function, and commit only after it returns successfully.
    """

    auth = auth_service or CodexAuthService()
    catalog = catalog_service or ModelCatalogService()
    auth_timeout = float(getattr(auth, "timeout_seconds", 120.0))
    console.print(
        layout.muted(
            f"Checking the Codex dependency and ChatGPT account "
            f"(timeout {auth_timeout:g}s)…"
        )
    )
    status = auth.status(refresh=True)
    if status.dependency.state in (
        DependencyState.MISSING,
        DependencyState.INCOMPATIBLE,
    ):
        installer = dependency_installer or CodexDependencyInstaller()
        try:
            plan = installer.resolve_plan()
        except CodexDependencyInstallError as exc:
            raise ChatGPTSetupError(
                f"Codex CLI is required, but an install plan could not be verified: {exc}. "
                f"Official fallback: {CODEX_OFFICIAL_INSTALL_COMMAND}",
                kind=SetupFailureKind.DEPENDENCY,
            ) from exc
        reason = (
            "not installed"
            if status.dependency.state is DependencyState.MISSING
            else f"incompatible ({status.dependency.version or 'version unknown'})"
        )
        console.print(f"{layout.warn(grammar.GLYPH_WARN)} OpenAI Codex CLI is {reason}.")
        console.print(f"{layout.field('Purpose')} ChatGPT subscription authentication and model access")
        console.print(f"{layout.field('Version/channel')} {plan.version} ({plan.channel})")
        console.print(f"{layout.field('Source', plan.source)} — {layout.escape(plan.metadata_url)}")
        console.print(f"{layout.field('Destination', plan.destination)}")
        console.print(f"{layout.field('Verification')} official metadata + SHA-256 manifest")
        ask_install = confirm_install or (
            lambda: Confirm.ask("Install the official standalone Codex CLI now?", default=True)
        )
        if not ask_install():
            raise ChatGPTSetupError(
                "Codex CLI installation was declined. Setup is incomplete. "
                f"Official manual command: {CODEX_OFFICIAL_INSTALL_COMMAND}",
                kind=SetupFailureKind.CANCELLED,
            )
        try:
            result = installer.install(
                plan,
                on_progress=lambda stage: console.print(
                    layout.muted(f"Codex dependency: {stage}…")
                ),
            )
        except CodexDependencyInstallError as exc:
            raise ChatGPTSetupError(
                f"Codex CLI installation did not complete: {exc}. "
                f"Official fallback: {CODEX_OFFICIAL_INSTALL_COMMAND}",
                kind=SetupFailureKind.DEPENDENCY,
            ) from exc
        console.print(
            f"{layout.ok(grammar.GLYPH_OK)} Verified Codex CLI {layout.escape(result.smoke_version)} at "
            f"{layout.strong(result.executable)}"
        )
        auth = CodexAuthService(command=result.executable)
        status = auth.status(refresh=True)

    if not status.ready:
        if status.error is not None:
            console.print(f"{layout.warn(grammar.GLYPH_WARN)} {layout.escape(redact_secrets(status.error.message))}")
        ask = confirm_login or (
            lambda: Confirm.ask("Sign in with your ChatGPT subscription now?", default=True)
        )
        if not ask():
            raise ChatGPTSetupError(
                "ChatGPT sign-in is required before this configuration can be saved.",
                kind=SetupFailureKind.CANCELLED,
            )
        method = login_method or detect_login_method()
        try:
            status = login_interactive(auth, method=method, console=console)
        except AuthServiceError as exc:
            detail = exc.status.error.message if exc.status.error else exc.status.state.value
            raise ChatGPTSetupError(
                f"ChatGPT sign-in did not complete: {redact_secrets(detail)}",
                kind=_auth_failure_kind(exc.status),
            ) from exc

    if not status.ready:
        detail = status.error.message if status.error else status.state.value
        raise ChatGPTSetupError(
            f"ChatGPT account verification failed: {detail}",
            kind=_auth_failure_kind(status),
        )

    provider = ProviderConfig(type=ProviderType.CODEX_SUBSCRIPTION)
    console.print(layout.muted("Loading the models available to this ChatGPT account…"))
    snapshot = catalog.get_catalog(
        "codex_subscription",
        provider,
        include_hidden=False,
        allow_stale_cache=True,
        codex_command=auth.command,
        cwd=auth.cwd,
    )
    if not snapshot.availability_verified:
        detail = snapshot.error.message if snapshot.error else snapshot.provenance_label
        raise ChatGPTSetupError(
            "Could not verify the models available to this ChatGPT account: "
            f"{redact_secrets(detail)}. Check the network, then retry setup.",
            kind=SetupFailureKind.MODEL,
        )
    selected = snapshot.default_entry()
    if selected is None:
        raise ChatGPTSetupError(
            "ChatGPT authentication succeeded, but the account model catalog is empty.",
            kind=SetupFailureKind.MODEL,
        )
    effort = selected.default_reasoning_effort
    ok, message = catalog.validate_selection(
        snapshot,
        selected.ref,
        reasoning_effort=effort,
    )
    if not ok:
        raise ChatGPTSetupError(message, kind=SetupFailureKind.MODEL)

    console.print(
        f"{layout.ok(grammar.GLYPH_OK)} Verified ChatGPT ({layout.strong(status.plan_type or 'plan unknown')})"
    )
    effort_text = f", reasoning {layout.strong(effort)}" if effort else ""
    console.print(
        f"{layout.ok(grammar.GLYPH_OK)} Using account default {layout.strong(selected.display_name)}"
        f"{effort_text} {layout.muted('(' + snapshot.provenance_label + ')')}"
    )
    return ChatGPTSetupResult(
        auth=status,
        catalog=snapshot,
        model=selected,
        reasoning_effort=effort,
    )


__all__ = ["ChatGPTSetupError", "ChatGPTSetupResult", "prepare_chatgpt_setup"]
