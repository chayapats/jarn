"""Typed setup failures shared by onboarding surfaces and the CLI boundary."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from jarn.errors import ErrorCode, ErrorDetail, error_detail
from jarn.exit_codes import (
    EXIT_AUTH,
    EXIT_CANCELLED,
    EXIT_INTERNAL,
    EXIT_MODEL_UNAVAILABLE,
    EXIT_NETWORK_PROVIDER,
    EXIT_TIMEOUT,
    EXIT_USAGE_CONFIG,
    EXIT_VERIFICATION_FAILED,
)


class SetupFailureKind(str, Enum):
    """Failure classes that callers must not collapse into a generic cancel."""

    CANCELLED = "cancelled"
    CONFIG = "config"
    AUTH = "auth"
    DEPENDENCY = "dependency"
    MODEL = "model"
    NETWORK = "network"
    TIMEOUT = "timeout"
    VERIFICATION = "verification"
    INTERNAL = "internal"


_EXIT_CODES = {
    SetupFailureKind.CANCELLED: EXIT_CANCELLED,
    SetupFailureKind.CONFIG: EXIT_USAGE_CONFIG,
    SetupFailureKind.AUTH: EXIT_AUTH,
    SetupFailureKind.DEPENDENCY: EXIT_VERIFICATION_FAILED,
    SetupFailureKind.MODEL: EXIT_MODEL_UNAVAILABLE,
    SetupFailureKind.NETWORK: EXIT_NETWORK_PROVIDER,
    SetupFailureKind.TIMEOUT: EXIT_TIMEOUT,
    SetupFailureKind.VERIFICATION: EXIT_VERIFICATION_FAILED,
    SetupFailureKind.INTERNAL: EXIT_INTERNAL,
}

_ERROR_CODES = {
    SetupFailureKind.CANCELLED: ErrorCode.CANCELLED,
    SetupFailureKind.CONFIG: ErrorCode.CONFIG_WRITE_FAILED,
    SetupFailureKind.AUTH: ErrorCode.AUTH_LOGIN_FAILED,
    SetupFailureKind.DEPENDENCY: ErrorCode.AUTH_DEPENDENCY_MISSING,
    SetupFailureKind.MODEL: ErrorCode.MODEL_UNAVAILABLE,
    SetupFailureKind.NETWORK: ErrorCode.NETWORK_FAILED,
    SetupFailureKind.TIMEOUT: ErrorCode.NETWORK_FAILED,
    SetupFailureKind.VERIFICATION: ErrorCode.VERIFICATION_FAILED,
    SetupFailureKind.INTERNAL: ErrorCode.INTERNAL,
}

_SUMMARIES = {
    SetupFailureKind.CANCELLED: "Setup was cancelled",
    SetupFailureKind.CONFIG: "Setup could not activate the configuration",
    SetupFailureKind.AUTH: "Setup could not verify authentication",
    SetupFailureKind.DEPENDENCY: "Setup could not verify a required dependency",
    SetupFailureKind.MODEL: "Setup could not verify the selected model",
    SetupFailureKind.NETWORK: "Setup could not reach a required service",
    SetupFailureKind.TIMEOUT: "Setup timed out",
    SetupFailureKind.VERIFICATION: "Setup readiness verification failed",
    SetupFailureKind.INTERNAL: "Setup failed unexpectedly",
}

_ACTIONS = {
    SetupFailureKind.CANCELLED: "Resume safely with `jarn setup`.",
    SetupFailureKind.CONFIG: "Run `jarn config validate`, fix the reported issue, then retry `jarn setup`.",
    SetupFailureKind.AUTH: "Run `jarn auth status`, then `jarn auth login` or `jarn auth repair` before retrying setup.",
    SetupFailureKind.DEPENDENCY: "Run `jarn auth repair`, verify the Codex dependency, then retry `jarn setup`.",
    SetupFailureKind.MODEL: "Run `jarn doctor --network` or choose an available model, then retry setup.",
    SetupFailureKind.NETWORK: "Check DNS, proxy, TLS, and provider reachability, then retry `jarn setup`.",
    SetupFailureKind.TIMEOUT: "Check the network and retry `jarn setup`; use device login on a headless host.",
    SetupFailureKind.VERIFICATION: "Run `jarn doctor`, resolve the failed readiness check, then retry `jarn setup`.",
    SetupFailureKind.INTERNAL: "Run `jarn doctor --report`, then retry or include the report when requesting support.",
}


class SetupCommandError(RuntimeError):
    """A non-successful setup result with a stable public error contract."""

    def __init__(
        self,
        message: str,
        *,
        kind: SetupFailureKind = SetupFailureKind.VERIFICATION,
        retryable: bool = True,
        action: str | None = None,
    ) -> None:
        self.kind = kind
        self.retryable = retryable
        self.action = action or _ACTIONS[kind]
        super().__init__(message)

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self.kind]

    @property
    def detail(self) -> ErrorDetail:
        return error_detail(
            _ERROR_CODES[self.kind],
            _SUMMARIES[self.kind],
            cause=str(self) or self.kind.value,
            component="onboarding",
            retryable=self.retryable,
            action=self.action,
            details={"stage": self.kind.value},
        )


def return_or_raise_setup_failure(
    error: SetupCommandError,
    *,
    propagate_errors: bool,
) -> Path | None:
    """Keep the legacy ``None`` API while allowing the CLI to preserve taxonomy."""

    if propagate_errors:
        raise error
    return None


__all__ = [
    "SetupCommandError",
    "SetupFailureKind",
    "return_or_raise_setup_failure",
]
