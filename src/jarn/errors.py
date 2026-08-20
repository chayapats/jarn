"""Stable, structured user-facing errors.

Every blocking boundary should eventually raise or render :class:`JarnUserError`
instead of inventing prose.  The object deliberately carries the complete error
anatomy required by the CLI, JSON mode, doctor, and support reports while keeping
all strings behind the central secret redactor.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


def _redact_structure(value: Any, *, known: set[str] | None = None) -> Any:
    """Import the config redactor only after this low-level module is ready.

    ``jarn config`` migrations themselves use :mod:`jarn.errors`. Importing the
    config package while this module is still initializing creates a cycle on a
    cold argparse failure, turning an ordinary usage error into an internal
    error. The redactor stays the single implementation; only its import is
    deferred until an ``ErrorDetail`` is actually rendered.
    """

    from jarn.config.secrets import redact_structure

    return redact_structure(value, known=known)


class ErrorCode(str, Enum):
    """Stable public codes.  Values are API: rename only with a compatibility alias."""

    CONFIG_INVALID_YAML = "JARN-CONFIG-001"
    CONFIG_INVALID_SCHEMA = "JARN-CONFIG-002"
    CONFIG_UNSUPPORTED_VERSION = "JARN-CONFIG-003"
    CONFIG_MIGRATION_FAILED = "JARN-CONFIG-004"
    CONFIG_WRITE_FAILED = "JARN-CONFIG-005"
    DOCTOR_CHECK_FAILED = "JARN-DOCTOR-001"
    DOCTOR_REPAIR_UNSAFE = "JARN-DOCTOR-002"
    DOCTOR_REPORT_FAILED = "JARN-DOCTOR-003"
    AUTH_FAILED = "JARN-AUTH-001"
    AUTH_DEPENDENCY_MISSING = "JARN-AUTH-001"
    AUTH_DEPENDENCY_INCOMPATIBLE = "JARN-AUTH-002"
    AUTH_SIGNED_OUT = "JARN-AUTH-003"
    AUTH_BILLING_MODE_MISMATCH = "JARN-AUTH-004"
    AUTH_WORKSPACE_DENIED = "JARN-AUTH-005"
    AUTH_EXPIRED_OR_REVOKED = "JARN-AUTH-006"
    AUTH_REFRESH_FAILED = "JARN-AUTH-007"
    AUTH_NETWORK_UNAVAILABLE = "JARN-AUTH-008"
    AUTH_PROTOCOL_ERROR = "JARN-AUTH-009"
    AUTH_LOGIN_FAILED = "JARN-AUTH-010"
    MODEL_UNAVAILABLE = "JARN-MODEL-001"
    MODEL_CATALOG_UNAVAILABLE = "JARN-MODEL-002"
    GATEWAY_DEPENDENCY_MISSING = "JARN-GATEWAY-001"
    GATEWAY_CONFIG_INVALID = "JARN-GATEWAY-002"
    GATEWAY_CREDENTIAL_INVALID = "JARN-GATEWAY-003"
    GATEWAY_ALLOWLIST_INVALID = "JARN-GATEWAY-004"
    GATEWAY_RUNTIME_FAILED = "JARN-GATEWAY-005"
    PERMISSION_DENIED = "JARN-SAFE-001"
    UNKNOWN_TOOL_GATED = "JARN-SAFE-002"
    NETWORK_FAILED = "JARN-NET-001"
    CLI_USAGE = "JARN-CLI-001"
    CANCELLED = "JARN-CLI-002"
    UPDATE_FAILED = "JARN-UPDATE-001"
    BUDGET_EXCEEDED = "JARN-BUDGET-001"
    VERIFICATION_FAILED = "JARN-VERIFY-001"
    LOCALE_OUTPUT_INVALID = "JARN-I18N-001"
    INTERNAL = "JARN-INTERNAL-001"


def _default_log_path() -> str:
    try:
        from jarn.config.paths import global_logs_dir

        return str(global_logs_dir() / "jarn.log")
    except Exception:  # pragma: no cover - last-resort error construction
        return "~/.jarn/logs/jarn.log"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """Serializable anatomy of one blocking user-visible error."""

    code: str
    summary: str
    cause: str
    component: str
    retryable: bool
    action: str
    log_path: str
    log_available: bool
    report_path: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self, *, known_secrets: set[str] | None = None) -> dict[str, Any]:
        return _redact_structure(asdict(self), known=known_secrets)

    def render(
        self,
        *,
        known_secrets: set[str] | None = None,
        stream: Any | None = None,
        verbose: bool | None = None,
        locale: str | None = None,
    ) -> str:
        """Render this error for a human or a pipe.

        Non-TTY text is the stable support contract (JSON, CI, ``TERM=dumb``):
        full English anatomy, single newlines. A TTY shows title + next step
        (localized chrome). ``component`` / log path appear on a TTY only when
        *verbose* is true or ``--verbose`` is on a jarn argv.
        """
        safe = self.to_dict(known_secrets=known_secrets)
        target = stream if stream is not None else sys.stderr
        want_verbose = _error_verbose(verbose)
        loc = _error_locale(locale)
        if _error_is_tty(target):
            lines = _tty_verbose_lines(safe, loc) if want_verbose else _tty_quiet_lines(safe, loc)
            if not _error_tty_color(target):
                return "\n\n".join(lines)
            return "\n\n".join(_paint_tty_error_lines(lines, safe, loc))
        return "\n".join(_plain_anatomy_lines(safe))


def _error_is_tty(stream: Any) -> bool:
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())


def _error_tty_color(stream: Any) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return _error_is_tty(stream)


def _error_verbose(explicit: bool | None, argv: Sequence[str] | None = None) -> bool:
    if explicit is not None:
        return explicit
    return _jarn_cli_verbose(sys.argv if argv is None else argv)


def _jarn_cli_verbose(argv: Sequence[str]) -> bool:
    """True when ``--verbose`` is on a jarn invocation, not ``pytest --verbose``.

    ``pytest -m jarn --verbose`` is a marker expression, not ``python -m jarn``.
    """
    if "--verbose" not in argv:
        return False
    name = Path(str(argv[0])).name.lower() if argv else ""
    if name in {"jarn", "jarn.exe"}:
        return True
    # ``python -m jarn`` rewrites argv[0] to the package __main__.py path.
    if _jarn_package_entry(str(argv[0]) if argv else ""):
        return True
    if not _python_argv0(name):
        return False
    args = list(argv)
    try:
        module_at = args.index("-m")
    except ValueError:
        return False
    return module_at + 1 < len(args) and str(args[module_at + 1]) in {"jarn", "jarn.cli"}


def _jarn_package_entry(argv0: str) -> bool:
    path = Path(argv0)
    if path.name.lower() not in {"__main__.py", "cli.py"}:
        return False
    return path.parent.name.lower() == "jarn"


def _python_argv0(name: str) -> bool:
    stem = name[:-4] if name.endswith(".exe") else name
    return stem == "python" or stem.startswith(("python", "pypy"))


def _error_locale(locale: str | None) -> str:
    from jarn.tui.i18n import resolve_locale

    if locale in {"en", "th"}:
        return locale
    return resolve_locale()


def _plain_anatomy_lines(safe: dict[str, Any]) -> list[str]:
    """English full brick — pipes, CI, and ``TERM=dumb``."""
    retry = "yes" if safe["retryable"] else "no"
    lines = [
        f"{safe['code']}: {safe['summary']}",
        f"Cause: {safe['cause']}",
        f"Component: {safe['component']} (retryable: {retry})",
        f"Next: {safe['action']}",
        _plain_log_line(safe),
    ]
    if safe.get("report_path"):
        lines.append(f"Report: {safe['report_path']}")
    return lines


def _plain_log_line(safe: dict[str, Any]) -> str:
    if safe["log_available"]:
        return f"Log: {safe['log_path']}"
    return (
        "Log: unavailable for this failure "
        f"(expected location: {safe['log_path']}; use `jarn doctor --report FILE`)"
    )


def _tty_quiet_lines(safe: dict[str, Any], locale: str) -> list[str]:
    from jarn.tui.i18n import t

    return [
        f"{safe['code']}: {safe['summary']}",
        f"{t('error.next', locale)}: {safe['action']}",
    ]


def _tty_verbose_lines(safe: dict[str, Any], locale: str) -> list[str]:
    from jarn.tui.i18n import t

    retry = t("error.retryable.yes" if safe["retryable"] else "error.retryable.no", locale)
    log_body = (
        str(safe["log_path"])
        if safe["log_available"]
        else t("error.log_unavailable", locale, path=safe["log_path"])
    )
    lines = [
        f"{safe['code']}: {safe['summary']}",
        f"{t('error.cause', locale)}: {safe['cause']}",
        f"{t('error.component', locale)}: {safe['component']} (retryable: {retry})",
        f"{t('error.next', locale)}: {safe['action']}",
        f"{t('error.log', locale)}: {log_body}",
    ]
    if safe.get("report_path"):
        lines.append(f"{t('error.report', locale)}: {safe['report_path']}")
    return lines


def _paint_tty_error_lines(
    lines: list[str],
    safe: dict[str, Any],
    locale: str,
) -> list[str]:
    from jarn.tui import palette
    from jarn.tui.i18n import t

    next_label = t("error.next", locale)
    painted: list[str] = []
    for line in lines:
        if line.startswith(f"{safe['code']}:"):
            painted.append(f"{_ansi_fg(palette.C_ERROR, str(safe['code']))}: {safe['summary']}")
        elif line.startswith(f"{next_label}:"):
            painted.append(f"{_ansi_fg(palette.ACCENT, f'{next_label}:')} {safe['action']}")
        else:
            painted.append(line)
    return painted


def _ansi_fg(hex_color: str, text: str) -> str:
    raw = hex_color.lstrip("#")
    if len(raw) != 6:
        return text
    try:
        r, g, b = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return text
    return f"\x1b[38;2;{r};{g};{b}m{text}\x1b[0m"


def error_detail(
    code: ErrorCode | str,
    summary: str,
    *,
    cause: str,
    component: str,
    retryable: bool,
    action: str,
    log_path: str | Path | None = None,
    report_path: str | Path | None = None,
    details: dict[str, Any] | None = None,
    known_secrets: set[str] | None = None,
) -> ErrorDetail:
    """Build a fully-populated, redacted :class:`ErrorDetail`."""
    resolved_log_path = str(log_path) if log_path is not None else _default_log_path()
    try:
        log_available = Path(resolved_log_path).expanduser().is_file()
    except (OSError, ValueError):
        log_available = False
    raw = ErrorDetail(
        code=code.value if isinstance(code, ErrorCode) else str(code),
        summary=str(summary),
        cause=str(cause),
        component=str(component),
        retryable=bool(retryable),
        action=str(action),
        log_path=resolved_log_path,
        log_available=log_available,
        report_path=str(report_path) if report_path is not None else None,
        details=details,
    )
    safe = raw.to_dict(known_secrets=known_secrets)
    return ErrorDetail(**safe)


class JarnUserError(RuntimeError):
    """Exception carrying a stable, actionable public error contract."""

    def __init__(self, detail: ErrorDetail) -> None:
        self.detail = detail
        super().__init__(detail.render())

    @property
    def code(self) -> str:
        return self.detail.code

    def to_dict(self) -> dict[str, Any]:
        return self.detail.to_dict()


def config_error(
    code: ErrorCode,
    summary: str,
    *,
    cause: str,
    action: str,
    path: str | Path | None = None,
    retryable: bool = False,
) -> JarnUserError:
    """Convenience constructor shared by config loading/migration boundaries."""
    details = {"path": str(path)} if path is not None else None
    return JarnUserError(
        error_detail(
            code,
            summary,
            cause=cause,
            component="configuration",
            retryable=retryable,
            action=action,
            details=details,
        )
    )


__all__ = [
    "ErrorCode",
    "ErrorDetail",
    "JarnUserError",
    "config_error",
    "error_detail",
]
