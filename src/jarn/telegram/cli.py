"""User-facing Telegram gateway entry (``jarn gateway`` / ``python -m jarn.telegram``).

Requires the ``telegram`` extra (aiogram), ``gateway.enabled``, a resolved bot
token, and a non-empty ``allowed_user_ids`` allowlist. Production boots wire
:class:`~jarn.gateway.daemon.DaemonSupervisor` +
:class:`~jarn.gateway.sessions.SessionRouter` through
:class:`~jarn.telegram.backend.SessionRouterBackend`. Pass ``--fake-backend``
(or ``JARN_TELEGRAM_FAKE_BACKEND=1``) for an in-memory dry-run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from jarn.errors import ErrorCode, ErrorDetail, error_detail
from jarn.exit_codes import EXIT_INTERNAL, EXIT_USAGE_CONFIG
from jarn.telegram import require_aiogram

_log = logging.getLogger("jarn.telegram.cli")

__all__ = [
    "GatewaySettings",
    "build_backend",
    "load_gateway_settings",
    "main",
    "run_gateway_cli",
]


@dataclass(slots=True, frozen=True)
class GatewaySettings:
    """Resolved credentials + allowlist for a gateway process."""

    token: str
    allowed_user_ids: list[int]
    repos: list[Any]
    fake_backend: bool
    tool_progress: str = "off"
    tool_progress_cleanup: str = "delete"
    long_running_notifications: bool = True


def _gateway_detail(
    code: ErrorCode,
    summary: str,
    *,
    cause: str,
    retryable: bool,
    action: str,
    component: str = "Telegram gateway",
) -> ErrorDetail:
    """Build the shared, centrally-redacted blocking error contract."""

    return error_detail(
        code,
        summary,
        cause=cause,
        component=component,
        retryable=retryable,
        action=action,
    )


def _print_gateway_error(detail: ErrorDetail) -> None:
    print(detail.render(), file=sys.stderr)


def _raise_gateway_error(detail: ErrorDetail) -> NoReturn:
    _print_gateway_error(detail)
    raise SystemExit(EXIT_USAGE_CONFIG)


def load_gateway_settings(
    *,
    fake_backend: bool = False,
    env: dict[str, str] | None = None,
    config: Any | None = None,
) -> GatewaySettings:
    """Load config and validate gateway prerequisites.

    Raises :class:`SystemExit` with code 2 on configuration / extra errors after
    rendering the same stable error anatomy as the top-level CLI. Prefer calling
    :func:`run_gateway_cli` from operators; this helper is exposed for tests.
    """
    environ = os.environ if env is None else env

    try:
        require_aiogram()
    except ImportError as exc:
        _raise_gateway_error(
            _gateway_detail(
                ErrorCode.GATEWAY_DEPENDENCY_MISSING,
                "The Telegram gateway dependency is not installed.",
                cause=str(exc),
                retryable=False,
                action=(
                    "Install the matching optional extra with "
                    "`pip install 'jarn[telegram]'`, then rerun `jarn gateway`."
                ),
                component="Telegram dependency",
            )
        )

    token = (environ.get("JARN_TELEGRAM_BOT_TOKEN") or "").strip()
    allowed: list[int] = []
    allowed_raw = (environ.get("JARN_TELEGRAM_ALLOWED_USER_IDS") or "").strip()
    if allowed_raw:
        for part in allowed_raw.split(","):
            part = part.strip()
            if part:
                try:
                    allowed.append(int(part))
                except ValueError:
                    _raise_gateway_error(
                        _gateway_detail(
                            ErrorCode.GATEWAY_ALLOWLIST_INVALID,
                            "The Telegram operator allowlist is invalid.",
                            cause=("JARN_TELEGRAM_ALLOWED_USER_IDS contains a non-integer entry."),
                            retryable=False,
                            action=(
                                "Set JARN_TELEGRAM_ALLOWED_USER_IDS to a "
                                "comma-separated list of numeric Telegram user IDs."
                            ),
                            component="Telegram allowlist",
                        )
                    )

    from jarn.config.loader import load_config
    from jarn.config.secrets import SecretResolutionError, resolve

    if config is None:
        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001
            _raise_gateway_error(
                _gateway_detail(
                    ErrorCode.GATEWAY_CONFIG_INVALID,
                    "Failed to load the Telegram gateway configuration.",
                    cause=str(exc),
                    retryable=False,
                    action=(
                        "Run `jarn config validate`, repair the reported file, "
                        "then rerun `jarn gateway`."
                    ),
                    component="gateway configuration",
                )
            )
    else:
        cfg = config

    if not cfg.gateway.enabled:
        _raise_gateway_error(
            _gateway_detail(
                ErrorCode.GATEWAY_CONFIG_INVALID,
                "The Telegram gateway is disabled.",
                cause="gateway.enabled is false in the effective configuration.",
                retryable=False,
                action="Run `jarn gateway setup`; it configures and validates Telegram safely.",
                component="gateway configuration",
            )
        )

    tg = cfg.gateway.telegram
    if not token:
        ref = (tg.token or "").strip()
        if ref:
            try:
                resolved = resolve(ref)
            except SecretResolutionError as exc:
                _raise_gateway_error(
                    _gateway_detail(
                        ErrorCode.GATEWAY_CREDENTIAL_INVALID,
                        "Failed to resolve gateway.telegram.token.",
                        cause=str(exc),
                        retryable=False,
                        action=(
                            "Repair the configured keychain/environment/file "
                            "reference, then rerun `jarn gateway`."
                        ),
                        component="Telegram credential",
                    )
                )
            token = (resolved or "").strip()
    if not allowed:
        allowed = list(tg.allowed_user_ids)

    if not token:
        _raise_gateway_error(
            _gateway_detail(
                ErrorCode.GATEWAY_CREDENTIAL_INVALID,
                "Missing bot token.",
                cause=(
                    "Neither gateway.telegram.token nor "
                    "JARN_TELEGRAM_BOT_TOKEN resolved to a value."
                ),
                retryable=False,
                action=(
                    "Run `jarn gateway setup`; it stores the token outside YAML and "
                    "validates the bot before activation."
                ),
                component="Telegram credential",
            )
        )
    if not allowed:
        _raise_gateway_error(
            _gateway_detail(
                ErrorCode.GATEWAY_ALLOWLIST_INVALID,
                "The Telegram operator allowlist is empty.",
                cause=(
                    "gateway.telegram.allowed_user_ids is empty; the gateway "
                    "fails closed instead of accepting arbitrary users."
                ),
                retryable=False,
                action=(
                    "Run `jarn gateway setup`; it discovers your Telegram ID from a "
                    "private /start and creates a fail-closed allowlist."
                ),
                component="Telegram allowlist",
            )
        )

    env_fake = environ.get("JARN_TELEGRAM_FAKE_BACKEND", "").strip() == "1"
    return GatewaySettings(
        token=token,
        allowed_user_ids=allowed,
        repos=list(cfg.gateway.repos),
        fake_backend=bool(fake_backend or env_fake),
        tool_progress=getattr(tg, "tool_progress", "off") or "off",
        tool_progress_cleanup=getattr(tg, "tool_progress_cleanup", "delete") or "delete",
        long_running_notifications=bool(getattr(tg, "long_running_notifications", True)),
    )


def build_backend(
    *,
    fake_backend: bool,
    repos: Sequence[Any] | None = None,
) -> tuple[Any, Any]:
    """Construct a :class:`~jarn.telegram.backend.GatewayBackend`.

    Returns ``(backend, supervisor_or_None)``. When *fake_backend* is True,
    returns :class:`InMemoryGatewayBackend` and ``None``. Otherwise wires
    :class:`DaemonSupervisor` + :class:`SessionRouter` through
    :class:`SessionRouterBackend`; the caller must ``supervisor.shutdown()``.
    """
    if fake_backend:
        from jarn.telegram.backend import InMemoryGatewayBackend

        return InMemoryGatewayBackend(), None

    from jarn.gateway.daemon import DaemonSupervisor
    from jarn.gateway.sessions import SessionRouter
    from jarn.telegram.backend import SessionRouterBackend

    def _on_notice(chat_id: int, text: str) -> None:
        _log.info("notice chat=%s: %s", chat_id, text)

    supervisor = DaemonSupervisor()
    router = SessionRouter(
        supervisor,
        repos=repos,
        on_notice=_on_notice,
    )
    return SessionRouterBackend(router=router, supervisor=supervisor), supervisor


def run_gateway_cli(
    *,
    fake_backend: bool = False,
    env: dict[str, str] | None = None,
) -> int:
    """Validate config, build backend, long-poll until stop/conflict/lock."""
    from jarn.config.loader import load_config
    from jarn.observability import setup_logging

    try:
        cfg = load_config()
        setup_logging(cfg.observability.log_level)
    except Exception as exc:  # noqa: BLE001
        _print_gateway_error(
            _gateway_detail(
                ErrorCode.GATEWAY_CONFIG_INVALID,
                "Failed to load the Telegram gateway configuration.",
                cause=str(exc),
                retryable=False,
                action=(
                    "Run `jarn config validate`, repair the reported file, then "
                    "rerun `jarn gateway`."
                ),
                component="gateway configuration",
            )
        )
        return EXIT_USAGE_CONFIG

    try:
        settings = load_gateway_settings(fake_backend=fake_backend, env=env, config=cfg)
    except SystemExit as exc:
        return int(exc.code or 2)

    try:
        backend, supervisor = build_backend(
            fake_backend=settings.fake_backend,
            repos=settings.repos,
        )
    except Exception as exc:  # noqa: BLE001 - stable CLI boundary
        _print_gateway_error(
            _gateway_detail(
                ErrorCode.GATEWAY_RUNTIME_FAILED,
                "The Telegram gateway backend could not start.",
                cause=str(exc),
                retryable=True,
                action="Run `jarn doctor --report`, correct the reported cause, and retry.",
                component="gateway backend",
            )
        )
        return EXIT_INTERNAL
    if settings.fake_backend:
        _log.warning("Using InMemoryGatewayBackend (dry-run) — no workers will run")
    else:
        _log.info(
            "Gateway starting with DaemonSupervisor + SessionRouter (allowlist=%s)",
            settings.allowed_user_ids,
        )

    from jarn.telegram.bot import run_gateway_bot

    try:
        result = asyncio.run(
            run_gateway_bot(
                token=settings.token,
                allowed_user_ids=settings.allowed_user_ids,
                backend=backend,
                tool_progress=settings.tool_progress,
                tool_progress_cleanup=settings.tool_progress_cleanup,
                long_running_notifications=settings.long_running_notifications,
            )
        )
        if result != 0:
            from jarn.telegram.bot import (
                EXIT_CONFLICT,
                EXIT_LOCK_HELD,
                EXIT_UNAUTHORIZED,
            )

            if result == EXIT_CONFLICT:
                detail = _gateway_detail(
                    ErrorCode.GATEWAY_RUNTIME_FAILED,
                    "Another Telegram poller took this bot token.",
                    cause="Telegram returned a getUpdates conflict (409).",
                    retryable=False,
                    action="Stop the other poller, then rerun `jarn gateway` once.",
                    component="Telegram polling",
                )
            elif result == EXIT_LOCK_HELD:
                detail = _gateway_detail(
                    ErrorCode.GATEWAY_RUNTIME_FAILED,
                    "Another local gateway process is already active.",
                    cause="The gateway process lock is currently held.",
                    retryable=True,
                    action="Stop or inspect the active gateway process before retrying.",
                    component="gateway process lock",
                )
            elif result == EXIT_UNAUTHORIZED:
                detail = _gateway_detail(
                    ErrorCode.GATEWAY_CREDENTIAL_INVALID,
                    "Telegram rejected the configured bot token.",
                    cause="Token syntax or Telegram authentication failed.",
                    retryable=False,
                    action="Replace the token reference with a valid bot token and retry.",
                    component="Telegram credential",
                )
            else:
                detail = _gateway_detail(
                    ErrorCode.GATEWAY_RUNTIME_FAILED,
                    "The Telegram gateway stopped with an error.",
                    cause=f"The gateway returned process status {result}.",
                    retryable=True,
                    action="Run `jarn doctor --report`, inspect the gateway log, and retry.",
                )
            _print_gateway_error(detail)
        return result
    except Exception as exc:  # noqa: BLE001 - stable CLI boundary
        _print_gateway_error(
            _gateway_detail(
                ErrorCode.GATEWAY_RUNTIME_FAILED,
                "The Telegram gateway stopped unexpectedly.",
                cause=str(exc),
                retryable=True,
                action="Run `jarn doctor --report`, inspect the gateway log, and retry.",
            )
        )
        return EXIT_INTERNAL
    finally:
        if supervisor is not None:
            with contextlib.suppress(Exception):
                supervisor.shutdown()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarn gateway",
        description=(
            "Run the Telegram gateway long-poll bot. Requires the telegram "
            "extra, gateway.enabled, a bot token, and allowed_user_ids."
        ),
    )
    parser.add_argument(
        "--fake-backend",
        action="store_true",
        help=(
            "Dry-run with InMemoryGatewayBackend (no daemon workers). "
            "Also set by JARN_TELEGRAM_FAKE_BACKEND=1."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry for ``python -m jarn.telegram`` and thin CLI wrappers."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    return run_gateway_cli(fake_backend=bool(args.fake_backend))
