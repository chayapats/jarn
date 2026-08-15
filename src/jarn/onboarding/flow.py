"""Shared transactional completion gate for every onboarding surface.

The plain and Textual wizards only collect non-secret answers.  This module is
the sole place that verifies external prerequisites, stages the *entire*
configuration, obtains consent for a potentially billable request, and commits.
"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Confirm

from jarn.catalog import CatalogSource, ModelCatalogService
from jarn.config import paths
from jarn.config.defaults import CLOUD_PROVIDERS, DEFAULT_MODELS
from jarn.config.pydantic_schema import config_to_dataclass, parse_config_model
from jarn.install_state import InstallStateError, update_setup_status
from jarn.onboarding.chatgpt import ChatGPTSetupError, ChatGPTSetupResult, prepare_chatgpt_setup
from jarn.onboarding.completion import (
    SetupCompletionError,
    completion_from_setup,
    render_setup_completion,
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
    credential_storage_notice,
    rollback_activated_credential,
)
from jarn.onboarding.outcome import SetupCommandError, SetupFailureKind
from jarn.onboarding.state import clear_setup_state, save_setup_state
from jarn.tui import grammar, layout


class SetupFlowError(SetupCommandError):
    """Setup did not pass every completion gate; no new config remains active."""


def _route_failure_kind(errors: tuple[str, ...]) -> SetupFailureKind:
    """Classify catalog failures without hiding credential remediation."""

    auth_markers = (
        "api key",
        "auth",
        "credential",
        "http 401",
        "http 403",
        "sign-in",
        "signed out",
        "subscription",
    )
    lowered = "\n".join(errors).lower()
    return (
        SetupFailureKind.AUTH
        if any(marker in lowered for marker in auth_markers)
        else SetupFailureKind.MODEL
    )


def _validate_staged_routes(
    candidate: dict[str, Any],
    catalog_service: ModelCatalogService,
) -> None:
    """Fail closed unless every staged runtime route has fresh catalog evidence."""

    config = config_to_dataclass(parse_config_model(candidate))
    snapshots = catalog_service.get_catalogs_for_routes(
        config,
        allow_stale_cache=False,
    )
    valid, errors = catalog_service.validate_routes(config, snapshots)
    if valid:
        return
    detail = "; ".join(errors)
    raise SetupFlowError(
        "The staged model routes are not ready: "
        f"{detail}. Open Advanced setup to replace unavailable background routes, "
        "or repair the named provider credential/endpoint and retry.",
        kind=_route_failure_kind(errors),
    )


def _advanced_config_kwargs(answers: dict[str, str]) -> dict[str, Any]:
    """Convert resumable string answers into validated config-stage options."""

    if answers.get("_provider_group") != "advanced":
        return {}
    try:
        budget = float(answers.get("budget_per_session_usd", "5"))
        warn_pct = int(answers.get("budget_warn_at_pct", "80"))
    except ValueError as exc:
        raise SetupFlowError(
            "Advanced budget values are invalid; return to the budget step.",
            kind=SetupFailureKind.CONFIG,
        ) from exc
    hard_stop = answers.get("budget_hard_stop", "true").lower() == "true"
    fallback = [
        item.strip() for item in answers.get("routing_fallback", "").split(",") if item.strip()
    ]
    effort = answers.get("reasoning_effort")
    return {
        "mode": answers.get("permission_mode", "ask"),
        "reasoning_effort": effort if effort and effort != "default" else None,
        "routing_subagent": answers.get("routing_subagent") or answers.get("model"),
        "routing_summarizer": answers.get("routing_summarizer") or answers.get("model"),
        "routing_fallback": fallback,
        "budget_per_session_usd": budget,
        "budget_hard_stop": hard_stop,
        "budget_warn_at_pct": warn_pct,
    }


def set_setup_progress(status: str) -> None:
    """Synchronize managed installer state; unmanaged installs need no record."""

    try:
        update_setup_status(status)
    except InstallStateError as exc:
        raise SetupFlowError(
            f"Could not update the managed install record: {exc}",
            kind=SetupFailureKind.VERIFICATION,
        ) from exc


def mark_setup_incomplete() -> None:
    """Best-effort failure marker that never hides the original setup error."""

    with suppress(InstallStateError, OSError):
        update_setup_status("incomplete")


def finalize_setup(
    answers: dict[str, str],
    *,
    console: Console,
    state_path: Path | None = None,
    config_path: Path | None = None,
    pending_credentials: PendingCredentials | None = None,
) -> Path:
    """Verify, stage, validate, and atomically commit collected setup answers.

    The setup-progress record is deliberately cleared only after the config and
    (when present) installer manifest both verify successfully.
    """

    provider = answers.get("provider", "")
    if not provider:
        raise SetupFlowError("No provider was selected.", kind=SetupFailureKind.CONFIG)
    theme = answers.get("theme", "dark")
    auth_result: ChatGPTSetupResult | None = None
    pending_value = pending_credentials.get(provider) if pending_credentials else None
    activated: ActivatedCredential | None = None
    committed = None
    ephemeral_env_name: str | None = None
    advanced_kwargs = _advanced_config_kwargs(answers)
    stage_options: dict[str, Any] = {"reasoning_effort": answers.get("reasoning_effort")}
    stage_options.update(advanced_kwargs)

    save_setup_state("confirm", answers, path=state_path)
    try:
        if provider == "codex_subscription":
            auth_result = prepare_chatgpt_setup(console=console)
            answers["model"] = auth_result.model.ref
            if auth_result.reasoning_effort:
                answers["reasoning_effort"] = auth_result.reasoning_effort
            else:
                answers.pop("reasoning_effort", None)
            stage_options["reasoning_effort"] = answers.get("reasoning_effort")
            # Persist only the verified model choice and effort, never auth data.
            save_setup_state("confirm", answers, path=state_path)

        model = (
            answers.get("model")
            or DEFAULT_MODELS.get(provider, DEFAULT_MODELS["openrouter"])["main"]
        )
        if answers.get("_credential_pending") and pending_value is None:
            raise SetupFlowError(
                "The pasted API key was intentionally kept only in process memory and is no "
                "longer available. Re-enter it at the key step.",
                kind=SetupFailureKind.AUTH,
            )
        key_ref: str | None
        if pending_value is not None:
            # The schema accepts references only, including for an in-memory
            # candidate. Give the pending value a one-run environment reference
            # so catalog and validation code can resolve it without persisting it.
            # A random name avoids overwriting operator state and is removed in
            # the outer finally block on success, cancellation, or failure.
            while ephemeral_env_name is None or ephemeral_env_name in os.environ:
                ephemeral_env_name = f"JARN_SETUP_CREDENTIAL_{uuid.uuid4().hex.upper()}"
            os.environ[ephemeral_env_name] = pending_value
            key_ref = f"${{{ephemeral_env_name}}}"
        else:
            key_ref = answers.get("key_ref") if provider in CLOUD_PROVIDERS else None
        staged = stage_setup_config(
            config_path or paths.global_config_path(),
            provider=provider,
            api_key_ref=key_ref,
            model=model,
            theme=theme,
            base_url=answers.get("base_url"),
            **stage_options,
        )
        if answers.get("_provider_group") != "advanced" and not staged.source_exists:
            # A new Standard setup has validated only its chosen main model. Use
            # that same proven route for background work rather than silently
            # inventing static subagent/summarizer availability. Advanced setup
            # remains the explicit place to choose distinct routes.
            stage_options["routing_subagent"] = model
            stage_options["routing_summarizer"] = model
            stage_options["routing_fallback"] = []
            staged = stage_setup_config(
                config_path or paths.global_config_path(),
                provider=provider,
                api_key_ref=key_ref,
                model=model,
                theme=theme,
                base_url=answers.get("base_url"),
                **stage_options,
            )

        validation = "not required"
        catalog_service = ModelCatalogService()
        if provider in CLOUD_PROVIDERS and provider != "codex_subscription":
            parsed = config_to_dataclass(parse_config_model(staged.candidate))
            configured_provider = parsed.providers[provider]
            snapshot = catalog_service.get_catalog(
                provider,
                configured_provider,
                allow_stale_cache=False,
            )
            if snapshot.availability_verified:
                selected = next((entry for entry in snapshot.models if entry.ref == model), None)
                if selected is None and answers.get("_provider_group") != "advanced":
                    selected = snapshot.default_entry()
                    if selected is not None:
                        previous_model = model
                        model = selected.ref
                        answers["model"] = model
                        for route_key in ("routing_subagent", "routing_summarizer"):
                            if stage_options.get(route_key) == previous_model:
                                stage_options[route_key] = model
                        staged = stage_setup_config(
                            config_path or paths.global_config_path(),
                            provider=provider,
                            api_key_ref=key_ref,
                            model=model,
                            theme=theme,
                            base_url=answers.get("base_url"),
                            **stage_options,
                        )
                        parsed = config_to_dataclass(parse_config_model(staged.candidate))
                        configured_provider = parsed.providers[provider]
                        console.print(
                            f"{layout.ok(grammar.GLYPH_OK)} Using provider-reported default "
                            f"[b]{selected.display_name}[/b] "
                            f"{layout.muted('(' + snapshot.provenance_label + ')')}"
                        )
                if selected is None:
                    raise SetupFlowError(
                        f"The selected model {model} is not in {snapshot.provenance_label}. "
                        "Choose a reported model or return to the model step.",
                        kind=SetupFailureKind.MODEL,
                    )
                valid, detail = catalog_service.validate_selection(snapshot, model)
                if not valid:
                    raise SetupFlowError(detail, kind=SetupFailureKind.MODEL)
            console.print(
                f"\n{layout.warn('Required readiness validation (may be billable)')}: "
                "sends one real model request and may consume provider credits."
            )
            if not Confirm.ask("Send the validation request and finish setup?", default=False):
                raise SetupFlowError(
                    "Billable provider validation was declined. No request was sent and no "
                    "configuration was changed; setup remains resumable.",
                    kind=SetupFailureKind.CANCELLED,
                )
            # Imported lazily to avoid a wizard/flow import cycle.
            from jarn.onboarding.wizard import validate_config

            if not validate_config(provider, model, staged.candidate):
                raise SetupFlowError(
                    "Provider validation did not succeed. No configuration was changed; "
                    "check the key, endpoint, and model before retrying.",
                    kind=SetupFailureKind.MODEL,
                )
            if not snapshot.availability_verified:
                # For a provider with no documented non-billable list endpoint,
                # persist only this exact successful selection. The cache binds
                # it to a hash of key+endpoint and expires normally.
                catalog_service.record_billable_validation(
                    provider,
                    configured_provider,
                    model,
                )
                validation = (
                    "verified by one real provider request (may be billable; exact model only)"
                )
            else:
                validation = (
                    f"listed by {snapshot.provenance_label} and verified by one real "
                    "provider request (may be billable)"
                )
        elif provider == "codex_subscription":
            validation = "verified by ChatGPT account and live model catalog"
        elif provider in {"ollama", "lmstudio"}:
            parsed = config_to_dataclass(parse_config_model(staged.candidate))
            snapshot = catalog_service.get_catalog(
                provider,
                parsed.providers[provider],
                allow_stale_cache=False,
            )
            selected = next((entry for entry in snapshot.models if entry.ref == model), None)
            if (
                snapshot.source is not CatalogSource.LOCAL_LIVE
                or not snapshot.availability_verified
                or selected is None
            ):
                detail = snapshot.error.message if snapshot.error else snapshot.provenance_label
                raise SetupFlowError(
                    f"The local endpoint did not report the selected model {model}: {detail}. "
                    "Start the server/load the model, then retry setup.",
                    kind=SetupFailureKind.MODEL,
                )
            validation = f"verified by {snapshot.provenance_label}"

        # This gate runs after active-main validation so providers without a
        # documented non-billable list can reuse the exact, fresh, credential-
        # scoped billable-validation evidence recorded above. The staged
        # candidate still contains the active pasted key in process memory, so
        # no secret needs to be persisted before every route proves ready.
        _validate_staged_routes(staged.candidate, catalog_service)

        install = verify_install_identity()
        if pending_value is not None:
            activated = activate_pending_credential(provider, pending_value)
            answers["key_ref"] = activated.reference
            answers.pop("_credential_pending", None)
            # Re-stage against the still-unchanged source with the permanent
            # reference. The raw key existed only in the validation candidate.
            staged = stage_setup_config(
                config_path or paths.global_config_path(),
                provider=provider,
                api_key_ref=activated.reference,
                model=model,
                theme=theme,
                base_url=answers.get("base_url"),
                **stage_options,
            )
        committed = commit_staged_config(staged)
        try:
            set_setup_progress("complete")
            clear_setup_state(path=state_path)
        except (SetupFlowError, OSError) as exc:
            rollback_errors: list[str] = []
            try:
                rollback_setup_commit(committed)
            except SetupConfigError as rollback_exc:
                rollback_errors.append(f"config rollback: {rollback_exc}")
            if activated is not None:
                try:
                    rollback_activated_credential(activated)
                except CredentialActivationError as rollback_exc:
                    rollback_errors.append(f"credential rollback: {rollback_exc}")
                else:
                    activated = None
            mark_setup_incomplete()
            if rollback_errors:
                raise SetupFlowError(
                    f"The completion record failed ({exc}) and automatic rollback was "
                    f"incomplete: {'; '.join(rollback_errors)}. Stop and inspect the config, "
                    "backup, and credential reference before retrying.",
                    kind=SetupFailureKind.CONFIG,
                ) from exc
            raise SetupFlowError(
                f"The completion record could not be finalized; the previous config was restored: {exc}",
                kind=SetupFailureKind.VERIFICATION,
            ) from exc

        summary = completion_from_setup(
            install=install,
            config_path=committed.path,
            backup_path=committed.backup_path,
            provider=provider,
            model=model,
            model_display=auth_result.model.display_name if auth_result else model,
            reasoning_effort=answers.get("reasoning_effort"),
            permission_mode=staged.permission_mode,
            auth=auth_result.auth if auth_result else None,
            validation=validation,
        )
        if activated is not None:
            console.print(f"  {credential_storage_notice(activated)}")
            if pending_credentials is not None:
                pending_credentials.discard(provider)
        render_setup_completion(console, summary)
        return committed.path
    except (
        ChatGPTSetupError,
        SetupCompletionError,
        SetupConfigError,
        CredentialActivationError,
        SetupFlowError,
        OSError,
    ) as exc:
        if activated is not None and committed is None:
            try:
                rollback_activated_credential(activated)
            except CredentialActivationError as rollback_exc:
                mark_setup_incomplete()
                raise SetupFlowError(
                    f"{exc}. A newly staged credential also could not be removed: "
                    f"{rollback_exc}. The previous config is still active.",
                    kind=SetupFailureKind.AUTH,
                ) from rollback_exc
            answers.pop("key_ref", None)
            answers["_credential_pending"] = "memory"
            save_setup_state("key", answers, path=state_path)
        mark_setup_incomplete()
        if isinstance(exc, SetupFlowError):
            raise
        if isinstance(exc, ChatGPTSetupError):
            kind = exc.kind
        elif isinstance(exc, CredentialActivationError):
            kind = SetupFailureKind.AUTH
        elif isinstance(exc, (SetupConfigError, OSError)):
            kind = SetupFailureKind.CONFIG
        else:
            kind = SetupFailureKind.VERIFICATION
        raise SetupFlowError(str(exc), kind=kind) from exc
    finally:
        if ephemeral_env_name is not None:
            os.environ.pop(ephemeral_env_name, None)


__all__ = [
    "SetupFlowError",
    "finalize_setup",
    "mark_setup_incomplete",
    "set_setup_progress",
]
