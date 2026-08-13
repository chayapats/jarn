"""Unified Codex/ChatGPT authentication service.

This service owns dependency inspection, direct app-server login ceremonies,
post-login verification, status classification, and scoped logout.  It never
reads OAuth tokens and never treats a subprocess/result code as proof of login.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarn.auth.models import (
    AuthError,
    AuthState,
    AuthStatus,
    DependencyState,
    DependencyStatus,
    LoginChallenge,
    LoginMethod,
    WorkspaceStatus,
)
from jarn.codex_dependency import CODEX_MINIMUM_VERSION
from jarn.config.secrets import redact_secrets
from jarn.errors import ErrorCode
from jarn.providers.codex_subscription import (
    CodexAppServer,
    CodexProtocolError,
    CodexRPCError,
    CodexSubscriptionError,
    CodexUnavailableError,
    normalize_codex_command,
)
from jarn.util.process_env import external_command_env

_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")
AUTH_TIMEOUT_ENV = "JARN_AUTH_TIMEOUT_SECONDS"
DEFAULT_AUTH_TIMEOUT_SECONDS = 120.0
MIN_AUTH_TIMEOUT_SECONDS = 1.0
MAX_AUTH_TIMEOUT_SECONDS = 900.0


def resolve_auth_timeout_seconds(
    value: float | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Resolve the user-configurable auth deadline to a safe finite bound.

    Explicit constructor/CLI values win.  Otherwise ``JARN_AUTH_TIMEOUT_SECONDS``
    is used.  Invalid environment values fall back to the documented default;
    valid values are clamped so an accidental setting can neither disable the
    timeout nor keep a terminal blocked indefinitely.
    """

    raw: float | str | None = value
    if raw is None:
        env = os.environ if environ is None else environ
        raw = env.get(AUTH_TIMEOUT_ENV)
    if raw is None or raw == "":
        return DEFAULT_AUTH_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_AUTH_TIMEOUT_SECONDS
    if not math.isfinite(parsed):
        return DEFAULT_AUTH_TIMEOUT_SECONDS
    return min(MAX_AUTH_TIMEOUT_SECONDS, max(MIN_AUTH_TIMEOUT_SECONDS, parsed))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _numeric_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(value)
    if match is None:
        return None
    parts = match.group(1).split("-", 1)[0].split("+", 1)[0].split(".")
    if len(parts) != 3:
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def inspect_codex_dependency(
    command: str | Sequence[str] | None = None,
    *,
    minimum_version: str | None = None,
    timeout_seconds: float = 5.0,
) -> DependencyStatus:
    """Inspect the Codex executable without installing or changing anything.

    A successful ``--version`` probe is still marked ``available_unverified``;
    protocol compatibility becomes proven only after an app-server handshake.
    """

    try:
        argv = normalize_codex_command(command)
    except CodexUnavailableError as exc:
        return DependencyStatus(
            state=DependencyState.MISSING,
            minimum_version=minimum_version,
            detail=redact_secrets(str(exc)),
        )

    executable = argv[0]
    try:
        completed = subprocess.run(  # noqa: S603 - argv only, never a shell
            [*argv, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=external_command_env(),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except FileNotFoundError as exc:
        return DependencyStatus(
            state=DependencyState.MISSING,
            executable=executable,
            minimum_version=minimum_version,
            detail=redact_secrets(f"Codex CLI was not found: {exc}"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DependencyStatus(
            state=DependencyState.INCOMPATIBLE,
            executable=executable,
            minimum_version=minimum_version,
            detail=redact_secrets(f"Could not run Codex CLI: {exc}"),
        )

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    match = _VERSION_RE.search(output)
    version = match.group(1) if match else None
    if completed.returncode != 0:
        return DependencyStatus(
            state=DependencyState.INCOMPATIBLE,
            executable=executable,
            version=version,
            minimum_version=minimum_version,
            detail=redact_secrets(output or f"Codex --version exited {completed.returncode}"),
        )
    if minimum_version:
        actual = _numeric_version(version or "")
        required = _numeric_version(minimum_version)
        if actual is None or required is None or actual < required:
            return DependencyStatus(
                state=DependencyState.INCOMPATIBLE,
                executable=executable,
                version=version,
                minimum_version=minimum_version,
                detail=(
                    f"Codex CLI {version or 'unknown'} is older than the required "
                    f"{minimum_version}."
                ),
            )
    return DependencyStatus(
        state=DependencyState.AVAILABLE_UNVERIFIED,
        executable=executable,
        version=version,
        minimum_version=minimum_version,
        detail=None if version else "Codex CLI version could not be parsed.",
    )


def _workspace(account: dict[str, Any]) -> WorkspaceStatus | None:
    raw_workspace = account.get("workspace")
    workspace = raw_workspace if isinstance(raw_workspace, dict) else {}
    raw_id = (
        workspace.get("id")
        or account.get("workspaceId")
        or account.get("accountId")
        or account.get("id")
    )
    name = workspace.get("name") or account.get("workspaceName")
    id_hash = None
    if raw_id:
        id_hash = hashlib.sha256(str(raw_id).encode("utf-8")).hexdigest()[:16]
    if id_hash is None and not name:
        return None
    return WorkspaceStatus(id_hash=id_hash, name=str(name) if name else None)


def _compatible(dependency: DependencyStatus) -> DependencyStatus:
    return replace(dependency, state=DependencyState.COMPATIBLE, detail=None)


def _status_from_account(
    account: dict[str, Any] | None,
    dependency: DependencyStatus,
    *,
    checked_at: str,
) -> AuthStatus:
    if account is None:
        return AuthStatus(
            state=AuthState.SIGNED_OUT,
            dependency=dependency,
            checked_at=checked_at,
            auth_mode="signed_out",
        )
    mode = str(account.get("type") or "")
    if mode == "chatgpt":
        return AuthStatus(
            state=AuthState.AUTHENTICATED_CHATGPT,
            dependency=dependency,
            checked_at=checked_at,
            authenticated=True,
            ready=True,
            auth_mode="chatgpt",
            plan_type=str(account.get("planType")) if account.get("planType") else None,
            workspace=_workspace(account),
        )
    if mode == "apiKey":
        return AuthStatus(
            state=AuthState.AUTHENTICATED_API_KEY,
            dependency=dependency,
            checked_at=checked_at,
            authenticated=True,
            ready=False,
            auth_mode="api_key",
            error=AuthError(
                code=ErrorCode.AUTH_BILLING_MODE_MISMATCH.value,
                message=(
                    "Codex is authenticated with an API key, which uses separate API billing."
                ),
                cause="The selected ChatGPT subscription route is using Codex API-key auth.",
                recovery="Run `jarn auth logout`, then `jarn auth login` and choose ChatGPT.",
            ),
        )
    return AuthStatus(
        state=AuthState.UNKNOWN_PROTOCOL_ERROR,
        dependency=dependency,
        checked_at=checked_at,
        auth_mode=mode or "unknown",
        error=AuthError(
            code=ErrorCode.AUTH_PROTOCOL_ERROR.value,
            message="Codex returned an authentication mode J.A.R.N. does not understand.",
            cause=f"account/read reported mode {mode or 'unknown'}.",
            recovery="Update Codex and J.A.R.N., then run `jarn auth repair`.",
        ),
    )


def _exception_status(
    exc: BaseException,
    dependency: DependencyStatus,
    *,
    checked_at: str,
    refreshing: bool = False,
) -> AuthStatus:
    message = redact_secrets(str(exc))
    lowered = message.lower()
    if dependency.state is DependencyState.MISSING:
        state = AuthState.DEPENDENCY_MISSING
        code = ErrorCode.AUTH_DEPENDENCY_MISSING.value
        recovery = "Install a supported Codex CLI, then run `jarn auth repair`."
    elif isinstance(exc, CodexUnavailableError):
        state = AuthState.DEPENDENCY_INCOMPATIBLE
        code = ErrorCode.AUTH_DEPENDENCY_INCOMPATIBLE.value
        recovery = "Update or reinstall Codex, then run `jarn auth repair`."
        dependency = replace(dependency, state=DependencyState.INCOMPATIBLE, detail=message)
    elif isinstance(exc, CodexRPCError) and (
        exc.code == -32601 or "method not found" in lowered or "unsupported method" in lowered
    ):
        state = AuthState.DEPENDENCY_INCOMPATIBLE
        code = ErrorCode.AUTH_DEPENDENCY_INCOMPATIBLE.value
        recovery = "Update Codex, then run `jarn auth repair`."
        dependency = replace(dependency, state=DependencyState.INCOMPATIBLE, detail=message)
    elif "workspace" in lowered and any(
        token in lowered for token in ("denied", "forbidden", "unauthorized", "access")
    ):
        state = AuthState.WORKSPACE_DENIED
        code = ErrorCode.AUTH_WORKSPACE_DENIED.value
        recovery = "Choose an allowed workspace/account, then retry `jarn auth login`."
    elif any(token in lowered for token in ("expired", "revoked", "invalid refresh")):
        state = AuthState.EXPIRED_OR_REVOKED
        code = ErrorCode.AUTH_EXPIRED_OR_REVOKED.value
        recovery = "Run `jarn auth login` to authenticate again."
    elif refreshing:
        state = AuthState.REFRESH_FAILED
        code = ErrorCode.AUTH_REFRESH_FAILED.value
        recovery = "Check the network and retry `jarn auth status`; then run `jarn auth login`."
    elif isinstance(exc, TimeoutError) or any(
        token in lowered
        for token in ("network", "connection", "dns", "timed out", "timeout", "offline")
    ):
        state = AuthState.NETWORK_UNAVAILABLE
        code = ErrorCode.AUTH_NETWORK_UNAVAILABLE.value
        recovery = "Check the network and proxy settings, then retry."
    else:
        state = AuthState.UNKNOWN_PROTOCOL_ERROR
        code = ErrorCode.AUTH_PROTOCOL_ERROR.value
        recovery = "Update Codex and J.A.R.N., then run `jarn auth repair`."
    return AuthStatus(
        state=state,
        dependency=dependency,
        checked_at=checked_at,
        error=AuthError(
            code=code,
            message=message,
            cause=message,
            recovery=recovery,
            retryable=state
            in {
                AuthState.NETWORK_UNAVAILABLE,
                AuthState.REFRESH_FAILED,
            },
        ),
    )


class CodexLoginSession:
    """A two-stage login whose challenge can be rendered before waiting."""

    def __init__(self, service: CodexAuthService, method: LoginMethod) -> None:
        self.service = service
        self.method = method
        self.server: CodexAppServer | None = None
        self.challenge: LoginChallenge | None = None
        self._completed: dict[str, Any] | None = None
        self._account_updated = False

    def __enter__(self) -> CodexLoginSession:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _observe(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "account/login/completed":
            self._completed = params
        elif method == "account/updated":
            self._account_updated = True

    def open(self) -> CodexLoginSession:
        dependency = self.service.dependency_status()
        if dependency.state in (DependencyState.MISSING, DependencyState.INCOMPATIBLE):
            exc = CodexUnavailableError(dependency.detail or "Codex CLI is unavailable")
            raise AuthServiceError(
                _exception_status(exc, dependency, checked_at=self.service.now())
            )
        server = CodexAppServer(
            command=self.service.command,
            cwd=self.service.cwd,
            timeout_seconds=self.service.timeout_seconds,
        )
        try:
            server.__enter__()
            raw = server.start_login(
                device_code=self.method is LoginMethod.DEVICE_CODE,
                on_notification=self._observe,
            )
            url_key = "verificationUrl" if self.method is LoginMethod.DEVICE_CODE else "authUrl"
            raw_expiry = raw.get("expiresIn") or raw.get("expiresInSeconds")
            expiry = raw_expiry if isinstance(raw_expiry, int) and raw_expiry > 0 else None
            self.challenge = LoginChallenge(
                login_id=str(raw["loginId"]),
                method=self.method,
                url=str(raw[url_key]),
                user_code=str(raw["userCode"]) if raw.get("userCode") else None,
                expires_in_seconds=expiry,
            )
            self.server = server
            return self
        except AuthServiceError:
            server.close()
            raise
        except (CodexSubscriptionError, TimeoutError) as exc:
            server.close()
            dependency = self.service.dependency_status()
            raise AuthServiceError(
                _exception_status(exc, dependency, checked_at=self.service.now())
            ) from exc

    def wait(
        self,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> AuthStatus:
        server = self.server
        challenge = self.challenge
        if server is None or challenge is None:
            raise RuntimeError("login session has not been opened")
        if on_progress:
            on_progress("waiting_for_login")
        try:
            if self._completed is None:
                completed = server.wait_for(
                    lambda message: message.get("method") == "account/login/completed",
                    on_notification=self._observe,
                )
                self._observe(completed)
            completion = self._completed or {}
            event_login_id = completion.get("loginId")
            if event_login_id and event_login_id != challenge.login_id:
                raise CodexProtocolError("login completion id did not match the active login")
            if completion.get("success") is False or completion.get("error"):
                error = completion.get("error")
                detail = (
                    str(error.get("message") or error)
                    if isinstance(error, dict)
                    else str(error or "Codex login failed")
                )
                raise CodexProtocolError(detail)
            if not self._account_updated:
                updated = server.wait_for(
                    lambda message: message.get("method") == "account/updated",
                    on_notification=self._observe,
                )
                self._observe(updated)
            if on_progress:
                on_progress("verifying_account")
            account = server.account(refresh=True)
            dependency = _compatible(self.service.dependency_status())
            status = _status_from_account(
                account,
                dependency,
                checked_at=self.service.now(),
            )
            if status.state is not AuthState.AUTHENTICATED_CHATGPT:
                raise AuthServiceError(status)
            if on_progress:
                on_progress("authenticated")
            return status
        except AuthServiceError:
            raise
        except (CodexSubscriptionError, TimeoutError) as exc:
            dependency = self.service.dependency_status()
            raise AuthServiceError(
                _exception_status(exc, dependency, checked_at=self.service.now())
            ) from exc

    def pending_status(self) -> AuthStatus:
        """Return the explicit, credential-free state while this ceremony waits."""

        if self.server is None or self.challenge is None or self._completed is not None:
            raise RuntimeError("login session is not pending")
        return AuthStatus(
            state=AuthState.LOGIN_PENDING,
            dependency=_compatible(self.service.dependency_status()),
            checked_at=self.service.now(),
            authenticated=False,
            ready=False,
            auth_mode=self.method.value,
        )

    def close(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            challenge = self.challenge
            if challenge is not None and self._completed is None:
                # Ctrl+C/terminal closure must cancel the server-side ceremony,
                # but cleanup itself must never hide the original interruption.
                original_timeout = server.timeout_seconds
                server.timeout_seconds = min(original_timeout, 2.0)
                with suppress(CodexSubscriptionError, TimeoutError):
                    server.cancel_login(challenge.login_id)
                server.timeout_seconds = original_timeout
            server.close()


class AuthServiceError(RuntimeError):
    """An auth operation failed with a stable, machine-readable status."""

    def __init__(self, status: AuthStatus) -> None:
        self.status = status
        message = status.error.message if status.error else status.state.value
        super().__init__(message)


class CodexAuthService:
    """Single source of truth for Codex-managed ChatGPT authentication."""

    def __init__(
        self,
        *,
        command: str | Sequence[str] | None = None,
        cwd: str | Path | None = None,
        timeout_seconds: float | None = None,
        minimum_version: str | None = CODEX_MINIMUM_VERSION,
        now: Callable[[], str] = _now_iso,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = resolve_auth_timeout_seconds(timeout_seconds)
        self.minimum_version = minimum_version
        self.now = now

    def dependency_status(self) -> DependencyStatus:
        return inspect_codex_dependency(
            self.command,
            minimum_version=self.minimum_version,
            timeout_seconds=min(self.timeout_seconds, 5.0),
        )

    def status(self, *, refresh: bool = False) -> AuthStatus:
        """Read and classify the current account without making a model turn."""

        checked_at = self.now()
        dependency = self.dependency_status()
        if dependency.state in (DependencyState.MISSING, DependencyState.INCOMPATIBLE):
            exc = CodexUnavailableError(dependency.detail or "Codex CLI is unavailable")
            return _exception_status(exc, dependency, checked_at=checked_at)
        try:
            with CodexAppServer(
                command=self.command,
                cwd=self.cwd,
                timeout_seconds=self.timeout_seconds,
            ) as server:
                account = server.account(refresh=refresh)
            return _status_from_account(
                account,
                _compatible(dependency),
                checked_at=checked_at,
            )
        except (CodexSubscriptionError, TimeoutError) as exc:
            return _exception_status(
                exc,
                dependency,
                checked_at=checked_at,
                refreshing=refresh,
            )

    def begin_login(self, method: LoginMethod = LoginMethod.BROWSER) -> CodexLoginSession:
        return CodexLoginSession(self, method)

    def login(
        self,
        method: LoginMethod,
        *,
        on_challenge: Callable[[LoginChallenge], None],
        on_progress: Callable[[str], None] | None = None,
    ) -> AuthStatus:
        """Run a visible login ceremony and return only after account verification.

        ``on_challenge`` is required by design: a caller cannot use this convenience
        API without handling the URL/code that the user needs to see.
        """

        with self.begin_login(method) as session:
            assert session.challenge is not None
            on_challenge(session.challenge)
            return session.wait(on_progress=on_progress)

    def logout(self) -> AuthStatus:
        """Log out only the Codex-managed mechanism and verify signed-out state."""

        dependency = self.dependency_status()
        checked_at = self.now()
        if dependency.state in (DependencyState.MISSING, DependencyState.INCOMPATIBLE):
            exc = CodexUnavailableError(dependency.detail or "Codex CLI is unavailable")
            return _exception_status(exc, dependency, checked_at=checked_at)
        try:
            with CodexAppServer(
                command=self.command,
                cwd=self.cwd,
                timeout_seconds=self.timeout_seconds,
            ) as server:
                server.logout()
                account = server.account(refresh=False)
            return _status_from_account(
                account,
                _compatible(dependency),
                checked_at=self.now(),
            )
        except (CodexSubscriptionError, TimeoutError) as exc:
            return _exception_status(exc, dependency, checked_at=self.now())


__all__ = [
    "AuthServiceError",
    "CodexAuthService",
    "CodexLoginSession",
    "inspect_codex_dependency",
    "resolve_auth_timeout_seconds",
]
