"""Stable, UI-independent authentication state for J.A.R.N.

The objects in this module deliberately contain no credential material.  Their
``to_dict`` output is the versioned contract used by terminal UI, ``--json``,
doctor, installers, and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

AUTH_STATUS_SCHEMA_VERSION = 1
AUTH_CHALLENGE_SCHEMA_VERSION = 1


class DependencyState(str, Enum):
    """Readiness of the external Codex CLI dependency."""

    MISSING = "missing"
    AVAILABLE_UNVERIFIED = "available_unverified"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


class AuthState(str, Enum):
    """States callers must distinguish instead of collapsing into signed-in/out."""

    DEPENDENCY_MISSING = "dependency_missing"
    DEPENDENCY_INCOMPATIBLE = "dependency_incompatible"
    SIGNED_OUT = "signed_out"
    LOGIN_PENDING = "login_pending"
    AUTHENTICATED_CHATGPT = "authenticated_chatgpt"
    AUTHENTICATED_API_KEY = "authenticated_api_key"
    EXPIRED_OR_REVOKED = "expired_or_revoked"
    WORKSPACE_DENIED = "workspace_denied"
    REFRESH_FAILED = "refresh_failed"
    NETWORK_UNAVAILABLE = "network_unavailable"
    UNKNOWN_PROTOCOL_ERROR = "unknown_protocol_error"


class LoginMethod(str, Enum):
    BROWSER = "browser"
    DEVICE_CODE = "device_code"


@dataclass(frozen=True, slots=True)
class AuthError:
    code: str
    message: str
    recovery: str
    cause: str | None = None
    component: str = "authentication"
    retryable: bool = False
    log_path: str = "~/.jarn/logs/jarn.log"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "summary": self.message,
            "cause": self.cause or self.message,
            "component": self.component,
            "retryable": self.retryable,
            "recovery": self.recovery,
            "action": self.recovery,
            "log_path": self.log_path,
        }


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    state: DependencyState
    executable: str | None = None
    version: str | None = None
    minimum_version: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "executable": self.executable,
            "version": self.version,
            "minimum_version": self.minimum_version,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    """Non-secret workspace information returned by Codex when available."""

    id_hash: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"id_hash": self.id_hash, "name": self.name}


@dataclass(frozen=True, slots=True)
class AuthStatus:
    state: AuthState
    dependency: DependencyStatus
    checked_at: str
    authenticated: bool = False
    ready: bool = False
    auth_mode: str | None = None
    plan_type: str | None = None
    workspace: WorkspaceStatus | None = None
    error: AuthError | None = None
    schema_version: int = AUTH_STATUS_SCHEMA_VERSION
    provider: str = "codex_subscription"

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON-compatible auth-status schema."""

        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "state": self.state.value,
            "authenticated": self.authenticated,
            "ready": self.ready,
            "auth_mode": self.auth_mode,
            "plan_type": self.plan_type,
            "workspace": self.workspace.to_dict() if self.workspace else None,
            "dependency": self.dependency.to_dict(),
            "checked_at": self.checked_at,
            "error": self.error.to_dict() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class LoginChallenge:
    """Visible login material that must be rendered before waiting."""

    login_id: str
    method: LoginMethod
    url: str
    user_code: str | None = None
    expires_in_seconds: int | None = None
    schema_version: int = AUTH_CHALLENGE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "login_id": self.login_id,
            "method": self.method.value,
            "url": self.url,
            "user_code": self.user_code,
            "expires_in_seconds": self.expires_in_seconds,
            "waiting": True,
            "cancel_hint": "Press Ctrl+C to cancel",
        }


__all__ = [
    "AUTH_CHALLENGE_SCHEMA_VERSION",
    "AUTH_STATUS_SCHEMA_VERSION",
    "AuthError",
    "AuthState",
    "AuthStatus",
    "DependencyState",
    "DependencyStatus",
    "LoginChallenge",
    "LoginMethod",
    "WorkspaceStatus",
]
