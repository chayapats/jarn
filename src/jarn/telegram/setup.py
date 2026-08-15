"""Safe, interactive Telegram gateway onboarding and user-service controls."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ruamel.yaml import YAML

from jarn.config.paths import default_global_home, global_config_path, global_home
from jarn.config.secrets import redact_secrets, resolve
from jarn.errors import ErrorCode, ErrorDetail, error_detail
from jarn.exit_codes import (
    EXIT_AUTH,
    EXIT_CANCELLED,
    EXIT_INTERNAL,
    EXIT_NETWORK_PROVIDER,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USAGE_CONFIG,
)
from jarn.onboarding.config_commit import (
    SetupCommitResult,
    SetupConfigError,
    commit_staged_config,
    rollback_setup_commit,
    stage_gateway_config,
)
from jarn.onboarding.credentials import (
    ActivatedCredential,
    activate_pending_credential,
    credential_storage_notice,
    rollback_activated_credential,
)
from jarn.util.atomic import atomic_write_text
from jarn.util.process_env import external_command_env

__all__ = [
    "GatewayServiceManager",
    "TelegramBotIdentity",
    "TelegramOperator",
    "TelegramSetupClient",
    "run_gateway_service_action",
    "run_gateway_service_install",
    "run_gateway_setup",
    "run_gateway_status",
]


@dataclass(frozen=True, slots=True)
class TelegramBotIdentity:
    id: int
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class TelegramOperator:
    id: int
    username: str | None = None
    display_name: str = ""

    @property
    def label(self) -> str:
        handle = f"@{self.username}" if self.username else "no username"
        name = self.display_name or "Telegram user"
        return f"{name} ({handle}, ID {self.id})"


class SetupClient(Protocol):
    def prepare(self, token: str) -> tuple[TelegramBotIdentity, int | None]: ...

    def discover(
        self,
        token: str,
        *,
        offset: int | None,
        timeout_seconds: float,
    ) -> list[TelegramOperator]: ...


class TelegramSetupFailure(RuntimeError):
    def __init__(self, detail: ErrorDetail, exit_code: int) -> None:
        self.detail = detail
        self.exit_code = exit_code
        super().__init__(detail.render())


def _detail(
    code: ErrorCode,
    summary: str,
    *,
    cause: str,
    component: str,
    retryable: bool,
    action: str,
    known: set[str] | None = None,
) -> ErrorDetail:
    return error_detail(
        code,
        summary,
        cause=cause,
        component=component,
        retryable=retryable,
        action=action,
        known_secrets=known,
    )


def _telegram_failure(exc: BaseException, *, token: str) -> TelegramSetupFailure:
    """Classify an aiogram failure without ever rendering its token-bearing URL."""

    name = type(exc).__name__
    known = {token}
    if isinstance(exc, ImportError):
        return TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_DEPENDENCY_MISSING,
                "The Telegram gateway dependency is not installed.",
                cause="This Python environment does not include the telegram extra.",
                component="Telegram dependency",
                retryable=False,
                action="Install `jarn[telegram]`, then rerun `jarn gateway setup`.",
                known=known,
            ),
            EXIT_USAGE_CONFIG,
        )
    if name in {"TokenValidationError", "TelegramUnauthorizedError"}:
        return TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_CREDENTIAL_INVALID,
                "Telegram rejected the bot token.",
                cause="The token is malformed, revoked, or no longer authorized.",
                component="Telegram credential",
                retryable=False,
                action="Create or copy a current token from @BotFather, then rerun setup.",
                known=known,
            ),
            EXIT_AUTH,
        )
    if name == "TelegramConflictError":
        return TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_RUNTIME_FAILED,
                "Another Telegram poller is using this bot.",
                cause="Telegram returned a getUpdates conflict (409).",
                component="Telegram operator discovery",
                retryable=True,
                action="Stop the other bot process, send /start again, and retry setup.",
                known=known,
            ),
            EXIT_NETWORK_PROVIDER,
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_RUNTIME_FAILED,
                "Telegram operator discovery timed out.",
                cause="No new private /start message arrived before the bounded timeout.",
                component="Telegram operator discovery",
                retryable=True,
                action="Open the displayed bot, send /start, then rerun `jarn gateway setup`.",
                known=known,
            ),
            EXIT_TIMEOUT,
        )
    return TelegramSetupFailure(
        _detail(
            ErrorCode.GATEWAY_RUNTIME_FAILED,
            "Telegram could not be reached during setup.",
            cause=f"The Telegram API request failed ({name}).",
            component="Telegram API",
            retryable=True,
            action="Check DNS/proxy/TLS connectivity, then rerun `jarn gateway setup`.",
            known=known,
        ),
        EXIT_NETWORK_PROVIDER,
    )


class TelegramSetupClient:
    """Small synchronous facade over aiogram for the terminal wizard."""

    @staticmethod
    def _bot(token: str) -> Any:
        from jarn.telegram import require_aiogram

        require_aiogram()
        from aiogram import Bot

        return Bot(token=token)

    def prepare(self, token: str) -> tuple[TelegramBotIdentity, int | None]:
        async def _prepare() -> tuple[TelegramBotIdentity, int | None]:
            bot = self._bot(token)
            try:
                me = await asyncio.wait_for(bot.get_me(), timeout=15.0)
                info = await asyncio.wait_for(bot.get_webhook_info(), timeout=15.0)
                webhook_url = str(getattr(info, "url", "") or "")
                if webhook_url:
                    raise TelegramSetupFailure(
                        _detail(
                            ErrorCode.GATEWAY_CONFIG_INVALID,
                            "This bot currently has a webhook configured.",
                            cause="Long polling cannot safely share a bot with an active webhook.",
                            component="Telegram transport",
                            retryable=False,
                            action=(
                                "Remove the webhook from its current deployment first; "
                                "J.A.R.N. will never delete it automatically."
                            ),
                            known={token},
                        ),
                        EXIT_USAGE_CONFIG,
                    )
                # Establish a clean boundary: no stale /start may become the
                # operator merely because a long-unused bot had >100 updates.
                # Telegram confirms each drained page only when the next page
                # is requested with its successor offset.
                offset: int | None = None
                for _page in range(100):
                    pending = await asyncio.wait_for(
                        bot.get_updates(
                            offset=offset,
                            timeout=0,
                            limit=100,
                            allowed_updates=["message"],
                        ),
                        timeout=15.0,
                    )
                    update_ids = [
                        value
                        for update in pending
                        if isinstance(value := getattr(update, "update_id", None), int)
                    ]
                    if update_ids:
                        offset = max(update_ids) + 1
                    if len(pending) < 100:
                        break
                else:
                    raise RuntimeError("Telegram backlog exceeds the safe setup bound")
                identity = TelegramBotIdentity(
                    id=int(me.id),
                    username=str(getattr(me, "username", "") or ""),
                    display_name=str(getattr(me, "full_name", "") or "Telegram bot"),
                )
                if not identity.username:
                    raise RuntimeError("Telegram bot has no username")
                return identity, offset
            finally:
                await bot.session.close()

        try:
            return asyncio.run(_prepare())
        except TelegramSetupFailure:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise _telegram_failure(exc, token=token) from None

    def discover(
        self,
        token: str,
        *,
        offset: int | None,
        timeout_seconds: float,
    ) -> list[TelegramOperator]:
        async def _discover() -> list[TelegramOperator]:
            bot = self._bot(token)
            deadline = time.monotonic() + timeout_seconds
            next_offset = offset
            found: dict[int, TelegramOperator] = {}
            try:
                while time.monotonic() < deadline:
                    remaining = max(0.1, deadline - time.monotonic())
                    poll_timeout = max(1, min(10, int(remaining)))
                    updates = await asyncio.wait_for(
                        bot.get_updates(
                            offset=next_offset,
                            timeout=poll_timeout,
                            limit=100,
                            allowed_updates=["message"],
                        ),
                        timeout=min(remaining + 2.0, poll_timeout + 5.0),
                    )
                    for update in updates:
                        uid = getattr(update, "update_id", None)
                        if isinstance(uid, int):
                            next_offset = uid + 1
                        message = getattr(update, "message", None)
                        if message is None:
                            continue
                        chat = getattr(message, "chat", None)
                        user = getattr(message, "from_user", None)
                        text = str(getattr(message, "text", "") or "").strip()
                        if (
                            getattr(chat, "type", None) != "private"
                            or user is None
                            or bool(getattr(user, "is_bot", False))
                            or not text.lower().startswith("/start")
                        ):
                            continue
                        user_id = getattr(user, "id", None)
                        if not isinstance(user_id, int) or user_id <= 0:
                            continue
                        found[user_id] = TelegramOperator(
                            id=user_id,
                            username=getattr(user, "username", None),
                            display_name=str(getattr(user, "full_name", "") or ""),
                        )
                    if found:
                        return list(found.values())
                raise TimeoutError("operator discovery deadline elapsed")
            finally:
                await bot.session.close()

        try:
            return asyncio.run(_discover())
        except TelegramSetupFailure:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise _telegram_failure(exc, token=token) from None


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    available: bool
    installed: bool
    active: bool
    enabled: bool
    linger: bool | None
    detail: str = ""


class GatewayServiceManager:
    """Install and operate an owner-scoped systemd service without token files."""

    unit_name = "jarn-telegram.service"

    def __init__(
        self,
        *,
        home: Path | None = None,
        executable: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        platform_name: str | None = None,
    ) -> None:
        self.home = Path.home() if home is None else Path(home)
        self.executable = executable or self._current_executable()
        self.runner = runner
        self.platform_name = sys.platform if platform_name is None else platform_name
        self.unit_path = self.home / ".config/systemd/user" / self.unit_name

    @staticmethod
    def _current_executable() -> Path:
        if bool(getattr(sys, "frozen", False)):
            return Path(sys.executable).resolve()
        resolved = shutil.which("jarn")
        if resolved:
            return Path(resolved).resolve()
        return Path(sys.argv[0]).expanduser().resolve()

    def _run(
        self, args: Sequence[str], *, timeout: float = 15.0
    ) -> subprocess.CompletedProcess[str]:
        return self.runner(
            list(args),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=external_command_env(),
        )

    def status(self) -> ServiceStatus:
        if not self.platform_name.startswith("linux") or shutil.which("systemctl") is None:
            return ServiceStatus(False, self.unit_path.is_file(), False, False, None)
        try:
            probe = self._run(["systemctl", "--user", "show-environment"])
        except (OSError, subprocess.SubprocessError) as exc:
            return ServiceStatus(
                False, self.unit_path.is_file(), False, False, None, type(exc).__name__
            )
        if probe.returncode != 0:
            return ServiceStatus(
                False, self.unit_path.is_file(), False, False, None, "user manager unavailable"
            )
        active = self._run(["systemctl", "--user", "is-active", self.unit_name]).returncode == 0
        enabled = self._run(["systemctl", "--user", "is-enabled", self.unit_name]).returncode == 0
        linger: bool | None = None
        username = os.environ.get("USER")
        if username and shutil.which("loginctl"):
            result = self._run(["loginctl", "show-user", username, "--property=Linger", "--value"])
            if result.returncode == 0:
                linger = result.stdout.strip().lower() == "yes"
        return ServiceStatus(True, self.unit_path.is_file(), active, enabled, linger)

    @staticmethod
    def _quote(value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\0")):
            raise ValueError("service paths must not contain control characters")
        return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    def _working_directory(value: str) -> str:
        """Encode a path for systemd's non-command WorkingDirectory= field.

        Unlike ``ExecStart=``, ``WorkingDirectory=`` on supported systemd
        versions does not consistently strip quotes placed around only the
        value.  Ubuntu 22.04 consequently treated ``"/home/user"`` as a
        relative path and rejected the whole unit.  C-style escapes are part
        of the unit-file syntax and preserve spaces without adding quotes.

        Absolute-path validation uses ``Path.is_absolute()`` rather than a
        leading ``/`` so Windows CI hosts can still generate unit text from
        drive-absolute pytest paths.  The user service itself remains
        Linux-only; on POSIX this check is equivalent to ``startswith("/")``.
        """

        if not Path(value).is_absolute():
            raise ValueError("the service working directory must be absolute")
        if any(character in value for character in ("\n", "\r", "\0")):
            raise ValueError("service paths must not contain control characters")
        return (
            value.replace("%", "%%")
            .replace("\\", "\\\\")
            .replace(" ", "\\x20")
            .replace("\t", "\\x09")
            .replace('"', "\\x22")
            .replace("'", "\\x27")
        )

    @staticmethod
    def _failure_cause(
        summary: str,
        result: subprocess.CompletedProcess[str],
    ) -> RuntimeError:
        detail = (result.stderr or result.stdout or "").strip()
        detail = " ".join(detail.split())
        if len(detail) > 1_000:
            detail = detail[:997] + "..."
        if detail:
            return RuntimeError(f"{summary}: {detail}")
        return RuntimeError(f"{summary} (systemctl exit {result.returncode})")

    def unit_text(self) -> str:
        executable = self._quote(str(self.executable))
        working = self._working_directory(str(self.home))
        environment = ""
        try:
            if global_home().resolve() != default_global_home().resolve():
                environment = f"Environment={self._quote('JARN_HOME=' + str(global_home()))}\n"
        except OSError:
            environment = f"Environment={self._quote('JARN_HOME=' + str(global_home()))}\n"
        return (
            "[Unit]\n"
            "Description=J.A.R.N. Telegram gateway\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={working}\n"
            f"ExecStart={executable} gateway\n"
            f"{environment}"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "RestartPreventExitStatus=75 76 77\n"
            "KillMode=mixed\n"
            "TimeoutStopSec=30s\n"
            "UMask=0077\n"
            "NoNewPrivileges=true\n"
            "PrivateTmp=true\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def install_and_start(self) -> ServiceStatus:
        before = self.status()
        if not before.available:
            raise RuntimeError("the systemd user manager is unavailable")
        if self.unit_path.is_symlink():
            raise RuntimeError("refusing to replace a symlinked Telegram user unit")
        old_bytes = self.unit_path.read_bytes() if self.unit_path.is_file() else None
        old_mode = self.unit_path.stat().st_mode & 0o777 if old_bytes is not None else None
        atomic_write_text(self.unit_path, self.unit_text(), mode=0o600)
        try:
            reload_result = self._run(["systemctl", "--user", "daemon-reload"])
            if reload_result.returncode != 0:
                raise self._failure_cause(
                    "systemd rejected the generated user unit",
                    reload_result,
                )
            enable = self._run(
                ["systemctl", "--user", "enable", self.unit_name],
                timeout=30.0,
            )
            if enable.returncode != 0:
                raise self._failure_cause("systemd could not enable the gateway", enable)
            # `enable --now` does not restart an already-running unit. An
            # explicit restart is mandatory when reconfiguration changed the
            # bot token or operator allowlist.
            restart = self._run(
                ["systemctl", "--user", "restart", self.unit_name],
                timeout=30.0,
            )
            if restart.returncode != 0:
                raise self._failure_cause(
                    "systemd could not start or restart the gateway",
                    restart,
                )
            result = self.status()
            if not result.active or not result.enabled:
                raise RuntimeError("the gateway unit did not become active and enabled")
            return result
        except Exception:
            if old_bytes is None:
                with contextlib.suppress(OSError):
                    self.unit_path.unlink()
            else:
                atomic_write_text(
                    self.unit_path,
                    old_bytes.decode("utf-8"),
                    mode=old_mode,
                )
            with contextlib.suppress(Exception):
                self._run(["systemctl", "--user", "daemon-reload"])
            if old_bytes is None or not before.enabled:
                with contextlib.suppress(Exception):
                    self._run(["systemctl", "--user", "disable", self.unit_name])
            if old_bytes is None or not before.active:
                with contextlib.suppress(Exception):
                    self._run(["systemctl", "--user", "stop", self.unit_name])
            elif before.active:
                with contextlib.suppress(Exception):
                    self._run(["systemctl", "--user", "restart", self.unit_name])
            raise

    def action(self, action: str) -> ServiceStatus:
        before = self.status()
        if not before.available:
            raise RuntimeError("the systemd user manager is unavailable")
        if not before.installed:
            raise RuntimeError("the J.A.R.N. Telegram user service is not installed")
        verb = {"start": "start", "stop": "stop", "restart": "restart"}[action]
        result = self._run(["systemctl", "--user", verb, self.unit_name], timeout=30.0)
        if result.returncode != 0:
            raise RuntimeError(f"systemd could not {verb} the gateway")
        return self.status()


def _read_secret(*, token_stdin: bool, secret_reader: Callable[[str], str]) -> str:
    if not token_stdin and secret_reader is getpass.getpass and not sys.stdin.isatty():
        raise TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_CREDENTIAL_INVALID,
                "A hidden terminal is required for the bot token.",
                cause="Standard input is not an interactive terminal, so input echo is unsafe.",
                component="Telegram credential",
                retryable=False,
                action=(
                    "Use an interactive terminal, or pipe the token with "
                    "`--token-stdin --allowed-user ID --yes`."
                ),
            ),
            EXIT_USAGE_CONFIG,
        )
    value = sys.stdin.readline() if token_stdin else secret_reader("Bot token (input hidden): ")
    token = value.strip()
    if (
        not token
        or len(token) > 512
        or any(character.isspace() or ord(character) < 32 for character in token)
        or ":" not in token
    ):
        raise TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_CREDENTIAL_INVALID,
                "The Telegram bot token is not valid.",
                cause="The hidden input was empty or did not have Telegram bot-token syntax.",
                component="Telegram credential",
                retryable=False,
                action="Copy the complete token from @BotFather and retry.",
                known={token} if token else None,
            ),
            EXIT_AUTH,
        )
    return token


def _yes(answer: str, *, default: bool) -> bool:
    value = answer.strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def _gateway_is_configured(path: Path) -> bool:
    """Validate and inspect the global config without migrating or writing it."""

    try:
        loaded = YAML(typ="safe").load(path.read_bytes().decode("utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("configuration root is not a mapping")
        from jarn.config.pydantic_schema import migrate_config, parse_config_model

        parsed = parse_config_model(migrate_config(loaded))
    except Exception as exc:
        raise TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_CONFIG_INVALID,
                "The global configuration must be repaired before Telegram setup.",
                cause=f"The existing file is not valid ({type(exc).__name__}).",
                component="gateway configuration",
                retryable=False,
                action="Run `jarn config validate`, repair the reported field, and retry.",
            ),
            EXIT_USAGE_CONFIG,
        ) from None
    return bool(
        parsed.gateway.enabled
        and parsed.gateway.telegram.token
        and parsed.gateway.telegram.allowed_user_ids
    )


def _cancelled() -> TelegramSetupFailure:
    return TelegramSetupFailure(
        _detail(
            ErrorCode.CANCELLED,
            "Telegram gateway setup was cancelled.",
            cause="No gateway configuration or new credential was committed.",
            component="Telegram setup",
            retryable=True,
            action="Rerun `jarn gateway setup` whenever you are ready.",
        ),
        EXIT_CANCELLED,
    )


def _select_operator(
    operators: list[TelegramOperator],
    *,
    input_fn: Callable[[str], str],
) -> list[int]:
    if len(operators) == 1:
        operator = operators[0]
        print(f"Detected operator: {operator.label}")
        return [operator.id]
    print("More than one person sent /start. Choose the operator to allow:")
    for index, operator in enumerate(operators, 1):
        print(f"  {index}. {operator.label}")
    answer = input_fn("Operator number (or q to cancel): ").strip()
    if answer.lower() in {"q", "quit", "cancel"}:
        raise _cancelled()
    try:
        selected = operators[int(answer) - 1]
    except (ValueError, IndexError):
        raise TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_ALLOWLIST_INVALID,
                "No unambiguous Telegram operator was selected.",
                cause="The selection did not identify exactly one discovered account.",
                component="Telegram allowlist",
                retryable=True,
                action="Rerun setup and choose one of the numbered accounts.",
            ),
            EXIT_USAGE_CONFIG,
        ) from None
    return [selected.id]


def run_gateway_setup(
    *,
    token_stdin: bool = False,
    allowed_users: Sequence[int] | None = None,
    no_service: bool = False,
    assume_yes: bool = False,
    force: bool = False,
    timeout_seconds: float = 120.0,
    client: SetupClient | None = None,
    service_manager: GatewayServiceManager | None = None,
    input_fn: Callable[[str], str] = input,
    secret_reader: Callable[[str], str] = getpass.getpass,
) -> int:
    """Guide a user from BotFather token to a verified, runnable gateway."""

    activated: ActivatedCredential | None = None
    committed: SetupCommitResult | None = None
    token = ""
    try:
        config_path = global_config_path()
        if not config_path.is_file():
            raise TelegramSetupFailure(
                _detail(
                    ErrorCode.GATEWAY_CONFIG_INVALID,
                    "J.A.R.N. must be set up before Telegram.",
                    cause=f"No global configuration exists at {config_path}.",
                    component="gateway configuration",
                    retryable=False,
                    action="Run `jarn setup`, verify a model, then rerun `jarn gateway setup`.",
                ),
                EXIT_USAGE_CONFIG,
            )

        already_configured = _gateway_is_configured(config_path)
        if already_configured and not force and not assume_yes:
            answer = input_fn("Telegram is already configured. Replace its bot/allowlist? [y/N]: ")
            if not _yes(answer, default=False):
                raise _cancelled()

        print("\nTelegram gateway setup")
        print("1. Open https://t.me/BotFather in Telegram and create a bot with /newbot.")
        print("2. Copy the token BotFather gives you; it will be entered invisibly here.\n")
        token = _read_secret(token_stdin=token_stdin, secret_reader=secret_reader)
        setup_client = client or TelegramSetupClient()
        identity, offset = setup_client.prepare(token)
        print(f"Verified bot: @{identity.username} ({identity.display_name}, ID {identity.id})")

        explicit = sorted(set(int(value) for value in (allowed_users or [])))
        if explicit:
            if any(value <= 0 for value in explicit):
                raise TelegramSetupFailure(
                    _detail(
                        ErrorCode.GATEWAY_ALLOWLIST_INVALID,
                        "The Telegram operator allowlist is invalid.",
                        cause="Every --allowed-user value must be a positive numeric ID.",
                        component="Telegram allowlist",
                        retryable=False,
                        action="Correct the numeric IDs and rerun setup.",
                    ),
                    EXIT_USAGE_CONFIG,
                )
            operator_ids = explicit
            print("Using explicitly supplied operator ID(s): " + ", ".join(map(str, explicit)))
        else:
            if token_stdin:
                raise TelegramSetupFailure(
                    _detail(
                        ErrorCode.GATEWAY_ALLOWLIST_INVALID,
                        "Non-interactive setup needs an explicit operator ID.",
                        cause="--token-stdin was used without --allowed-user.",
                        component="Telegram allowlist",
                        retryable=False,
                        action="Add `--allowed-user YOUR_NUMERIC_ID` and retry.",
                    ),
                    EXIT_USAGE_CONFIG,
                )
            print(f"\n3. Open https://t.me/{identity.username} and send /start now.")
            answer = input_fn(
                "Press Enter after sending /start, or type a numeric user ID: "
            ).strip()
            if answer:
                try:
                    manual_id = int(answer)
                except ValueError:
                    raise TelegramSetupFailure(
                        _detail(
                            ErrorCode.GATEWAY_ALLOWLIST_INVALID,
                            "The Telegram operator ID is invalid.",
                            cause="The manual value was not a numeric Telegram user ID.",
                            component="Telegram allowlist",
                            retryable=False,
                            action="Send /start and press Enter, or enter the numeric ID only.",
                        ),
                        EXIT_USAGE_CONFIG,
                    ) from None
                if manual_id <= 0:
                    raise TelegramSetupFailure(
                        _detail(
                            ErrorCode.GATEWAY_ALLOWLIST_INVALID,
                            "The Telegram operator ID is invalid.",
                            cause="Telegram user IDs must be positive integers.",
                            component="Telegram allowlist",
                            retryable=False,
                            action="Enter the positive numeric ID and retry.",
                        ),
                        EXIT_USAGE_CONFIG,
                    )
                operator_ids = [manual_id]
            else:
                print("Waiting for a new private /start message…")
                operators = setup_client.discover(
                    token,
                    offset=offset,
                    timeout_seconds=max(5.0, min(float(timeout_seconds), 600.0)),
                )
                operator_ids = _select_operator(operators, input_fn=input_fn)

        print("\nReady to save:")
        print(f"  Bot: @{identity.username}")
        print("  Allowed operator ID(s): " + ", ".join(map(str, operator_ids)))
        print(f"  Config: {config_path}")
        print("  Token: OS keychain when available; private file fallback otherwise")
        if not assume_yes:
            answer = input_fn("Save this Telegram gateway configuration? [Y/n]: ")
            if not _yes(answer, default=True):
                raise _cancelled()

        activated = activate_pending_credential("telegram", token)
        staged = stage_gateway_config(
            config_path,
            token_ref=activated.reference,
            allowed_user_ids=operator_ids,
        )
        committed = commit_staged_config(staged)

        from jarn.config.loader import load_config

        verified = load_config(
            global_path=config_path,
            project_path=config_path,
            project_trusted=False,
            project_raw={},
        )
        if (
            not verified.gateway.enabled
            or verified.gateway.telegram.token != activated.reference
            or sorted(verified.gateway.telegram.allowed_user_ids) != operator_ids
            or resolve(activated.reference) != token
        ):
            raise SetupConfigError("post-commit gateway verification failed")

        manager = service_manager or GatewayServiceManager()
        service = manager.status()
        should_install = False
        if not no_service and service.available:
            if assume_yes:
                should_install = True
            else:
                should_install = _yes(
                    input_fn("Start automatically as a user service? [Y/n]: "),
                    default=True,
                )
        if should_install:
            try:
                running = manager.install_and_start()
            except Exception as exc:  # noqa: BLE001 - keep verified config usable
                cause = redact_secrets(str(exc), known={token})
                print(f"\nTelegram configuration verified at {config_path}")
                if committed.backup_path:
                    print(f"Previous config backed up at {committed.backup_path}")
                print(credential_storage_notice(activated))
                print("Run now in the foreground with: jarn gateway")
                service_detail = _detail(
                    ErrorCode.GATEWAY_RUNTIME_FAILED,
                    "Telegram is configured, but its user service could not start.",
                    cause=cause,
                    component="Telegram user service",
                    retryable=True,
                    action=(
                        "The verified Telegram configuration was kept. Run "
                        "`jarn gateway` now, or correct systemd and run "
                        "`jarn gateway install-service`."
                    ),
                    known={token},
                )
                print(service_detail.render(known_secrets={token}), file=sys.stderr)
                return EXIT_INTERNAL
            print(f"User service enabled and running: {manager.unit_path}")
            if running.linger is False:
                username = os.environ.get("USER", "YOUR_USER")
                print(
                    "To keep it running after logout, an administrator can run: "
                    f"sudo loginctl enable-linger {username}"
                )
        else:
            if not service.available and not no_service:
                print("A systemd user manager was not available; no service files were changed.")
        print(f"\nTelegram configuration verified at {config_path}")
        if committed.backup_path:
            print(f"Previous config backed up at {committed.backup_path}")
        print(credential_storage_notice(activated))
        if not should_install:
            print("Run in the foreground with: jarn gateway")
        print("Check anytime with: jarn gateway status")
        return EXIT_SUCCESS
    except (EOFError, KeyboardInterrupt):
        failure = _cancelled()
    except TelegramSetupFailure as exc:
        failure = exc
    except Exception as exc:  # noqa: BLE001 - stable setup boundary
        cause = redact_secrets(str(exc), known={token} if token else None)
        failure = TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_CONFIG_INVALID,
                "Telegram gateway setup could not be committed.",
                cause=cause,
                component="Telegram setup transaction",
                retryable=True,
                action="Correct the reported cause, then rerun `jarn gateway setup`.",
                known={token} if token else None,
            ),
            EXIT_INTERNAL,
        )

    rollback_errors: list[str] = []
    config_rollback_failed = False
    if committed is not None:
        try:
            rollback_setup_commit(committed)
        except Exception as exc:  # noqa: BLE001
            config_rollback_failed = True
            rollback_errors.append(
                "configuration rollback failed: "
                + redact_secrets(str(exc), known={token} if token else None)
            )
    # If config rollback failed, retain the referenced credential. Deleting it
    # would turn a recoverable committed config into a guaranteed broken one.
    if activated is not None and not config_rollback_failed:
        try:
            rollback_activated_credential(activated)
        except Exception as exc:  # noqa: BLE001
            rollback_errors.append(redact_secrets(str(exc), known={token} if token else None))
    if rollback_errors:
        failure = TelegramSetupFailure(
            _detail(
                ErrorCode.GATEWAY_CONFIG_INVALID,
                "Telegram setup failed and cleanup needs attention.",
                cause="; ".join(rollback_errors),
                component="Telegram setup rollback",
                retryable=True,
                action="Run `jarn doctor --report`, remove only the staged credential, and retry.",
            ),
            EXIT_INTERNAL,
        )
    print(failure.detail.render(known_secrets={token} if token else None), file=sys.stderr)
    return failure.exit_code


def run_gateway_status(*, service_manager: GatewayServiceManager | None = None) -> int:
    """Show a secret-free gateway/config/service summary."""

    try:
        from jarn.config.loader import load_config

        config_path = global_config_path()
        cfg = load_config(
            global_path=config_path,
            project_path=config_path,
            project_trusted=False,
            project_raw={},
        )
        tg = cfg.gateway.telegram
        manager = service_manager or GatewayServiceManager()
        status = manager.status()
        print("Telegram gateway status")
        print(f"  Config enabled: {'yes' if cfg.gateway.enabled else 'no'}")
        print(f"  Bot token reference: {'configured' if tg.token else 'missing'}")
        print(f"  Allowed operators: {len(tg.allowed_user_ids)}")
        if status.available:
            print(f"  User service installed: {'yes' if status.installed else 'no'}")
            print(f"  User service enabled: {'yes' if status.enabled else 'no'}")
            print(f"  User service active: {'yes' if status.active else 'no'}")
            if status.linger is not None:
                print(f"  Runs after logout (linger): {'yes' if status.linger else 'no'}")
        else:
            print("  User service: unavailable (run `jarn gateway` in foreground)")
        if not cfg.gateway.enabled or not tg.token or not tg.allowed_user_ids:
            print("  Next: jarn gateway setup")
            return EXIT_USAGE_CONFIG
        return EXIT_SUCCESS
    except Exception as exc:  # noqa: BLE001
        detail = _detail(
            ErrorCode.GATEWAY_CONFIG_INVALID,
            "Telegram gateway status could not be read.",
            cause=redact_secrets(str(exc)),
            component="Telegram gateway status",
            retryable=True,
            action="Run `jarn config validate`, then retry `jarn gateway status`.",
        )
        print(detail.render(), file=sys.stderr)
        return EXIT_USAGE_CONFIG


def run_gateway_service_action(
    action: str,
    *,
    service_manager: GatewayServiceManager | None = None,
) -> int:
    manager = service_manager or GatewayServiceManager()
    try:
        status = manager.action(action)
    except Exception as exc:  # noqa: BLE001
        detail = _detail(
            ErrorCode.GATEWAY_RUNTIME_FAILED,
            f"The Telegram gateway service could not {action}.",
            cause=redact_secrets(str(exc)),
            component="Telegram user service",
            retryable=True,
            action="Run `jarn gateway status`, correct the service state, and retry.",
        )
        print(detail.render(), file=sys.stderr)
        return EXIT_INTERNAL
    print(
        f"Telegram user service: active={'yes' if status.active else 'no'}, "
        f"enabled={'yes' if status.enabled else 'no'}"
    )
    return EXIT_SUCCESS


def run_gateway_service_install(
    *,
    service_manager: GatewayServiceManager | None = None,
) -> int:
    """Install and start the owner service from an existing verified config."""

    # Reuse the production config/secret/allowlist gate before creating a
    # persistent service. The resolved token stays in memory and is never
    # passed to systemd, argv, or the unit file.
    try:
        from jarn.telegram.cli import load_gateway_settings

        load_gateway_settings()
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE_CONFIG

    manager = service_manager or GatewayServiceManager()
    try:
        status = manager.install_and_start()
    except Exception as exc:  # noqa: BLE001
        detail = _detail(
            ErrorCode.GATEWAY_RUNTIME_FAILED,
            "The Telegram gateway user service could not be installed.",
            cause=redact_secrets(str(exc)),
            component="Telegram user service",
            retryable=True,
            action=(
                "Correct the reported systemd cause, then retry "
                "`jarn gateway install-service`; `jarn gateway` remains available."
            ),
        )
        print(detail.render(), file=sys.stderr)
        return EXIT_INTERNAL

    print(f"Telegram user service enabled and running: {manager.unit_path}")
    if status.linger is False:
        username = os.environ.get("USER", "YOUR_USER")
        print(
            "To keep it running after logout, an administrator can run: "
            f"sudo loginctl enable-linger {username}"
        )
    return EXIT_SUCCESS
