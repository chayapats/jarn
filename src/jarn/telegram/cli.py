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
from typing import Any

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


def load_gateway_settings(
    *,
    fake_backend: bool = False,
    env: dict[str, str] | None = None,
    config: Any | None = None,
) -> GatewaySettings:
    """Load config and validate gateway prerequisites.

    Raises :class:`SystemExit` with code 2 on configuration / extra errors
    (messages written to stderr). Prefer calling :func:`run_gateway_cli` from
    operators; this helper is exposed for tests.
    """
    environ = os.environ if env is None else env

    try:
        require_aiogram()
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

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
                    print(
                        f"Invalid user id in JARN_TELEGRAM_ALLOWED_USER_IDS: {part!r}",
                        file=sys.stderr,
                    )
                    raise SystemExit(2) from None

    from jarn.config.loader import load_config
    from jarn.config.secrets import SecretResolutionError, resolve

    if config is None:
        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to load config: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    else:
        cfg = config

    if not cfg.gateway.enabled:
        print(
            "gateway.enabled is false — enable it in ~/.jarn/config.yaml",
            file=sys.stderr,
        )
        raise SystemExit(2)

    tg = cfg.gateway.telegram
    if not token:
        ref = (tg.token or "").strip()
        if ref:
            try:
                resolved = resolve(ref)
            except SecretResolutionError as exc:
                print(
                    f"Failed to resolve gateway.telegram.token: {exc}",
                    file=sys.stderr,
                )
                raise SystemExit(2) from exc
            token = (resolved or "").strip()
    if not allowed:
        allowed = list(tg.allowed_user_ids)

    if not token:
        print(
            "Missing bot token. Set gateway.telegram.token or "
            "JARN_TELEGRAM_BOT_TOKEN.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not allowed:
        print(
            "gateway.telegram.allowed_user_ids is empty — deny-by-default; "
            "refusing to start.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    env_fake = environ.get("JARN_TELEGRAM_FAKE_BACKEND", "").strip() == "1"
    return GatewaySettings(
        token=token,
        allowed_user_ids=allowed,
        repos=list(cfg.gateway.repos),
        fake_backend=bool(fake_backend or env_fake),
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
        print(f"Failed to load config: {exc}", file=sys.stderr)
        return 2

    try:
        settings = load_gateway_settings(
            fake_backend=fake_backend, env=env, config=cfg
        )
    except SystemExit as exc:
        return int(exc.code or 2)

    backend, supervisor = build_backend(
        fake_backend=settings.fake_backend,
        repos=settings.repos,
    )
    if settings.fake_backend:
        _log.warning(
            "Using InMemoryGatewayBackend (dry-run) — no workers will run"
        )
    else:
        _log.info(
            "Gateway starting with DaemonSupervisor + SessionRouter "
            "(allowlist=%s)",
            settings.allowed_user_ids,
        )

    from jarn.telegram.bot import run_gateway_bot

    try:
        return asyncio.run(
            run_gateway_bot(
                token=settings.token,
                allowed_user_ids=settings.allowed_user_ids,
                backend=backend,
            )
        )
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
