from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from jarn.auth import (
    AUTH_STATUS_SCHEMA_VERSION,
    DEFAULT_AUTH_TIMEOUT_SECONDS,
    AuthServiceError,
    AuthState,
    CodexAuthService,
    DependencyState,
    LoginMethod,
    inspect_codex_dependency,
    login_interactive,
    resolve_auth_timeout_seconds,
)
from jarn.errors import ErrorCode

FAKE_SERVER = Path(__file__).with_name("codex_fake_app_server.py")
FAKE_COMMAND = (sys.executable, str(FAKE_SERVER))


def service(tmp_path: Path, *, timeout: float = 5.0) -> CodexAuthService:
    return CodexAuthService(
        command=FAKE_COMMAND,
        cwd=tmp_path,
        timeout_seconds=timeout,
        now=lambda: "2026-08-09T12:00:00Z",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_AUTH_TIMEOUT_SECONDS),
        ("", DEFAULT_AUTH_TIMEOUT_SECONDS),
        ("invalid", DEFAULT_AUTH_TIMEOUT_SECONDS),
        ("nan", DEFAULT_AUTH_TIMEOUT_SECONDS),
        ("0", 1.0),
        ("12.5", 12.5),
        ("9999", 900.0),
    ],
)
def test_auth_timeout_environment_is_finite_and_bounded(raw, expected):
    env = {} if raw is None else {"JARN_AUTH_TIMEOUT_SECONDS": raw}

    assert resolve_auth_timeout_seconds(environ=env) == expected


def test_dependency_missing_is_a_first_class_state(monkeypatch):
    monkeypatch.setattr("jarn.providers.codex_subscription.shutil.which", lambda _name: None)
    monkeypatch.setattr("jarn.codex_dependency.managed_codex_executable", lambda **_kwargs: None)
    status = inspect_codex_dependency()

    assert status.state is DependencyState.MISSING
    assert status.executable is None


def test_dependency_version_is_reported(tmp_path):
    status = service(tmp_path).dependency_status()

    assert status.state is DependencyState.AVAILABLE_UNVERIFIED
    assert status.version == "1.2.3"
    assert status.executable == sys.executable


def test_dependency_minimum_marks_old_cli_incompatible(monkeypatch):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "outdated")
    status = inspect_codex_dependency(FAKE_COMMAND, minimum_version="0.5.0")

    assert status.state is DependencyState.INCOMPATIBLE
    assert status.version == "0.1.0"
    assert "older" in (status.detail or "")


def test_explicit_missing_dependency_path_is_missing(tmp_path):
    status = inspect_codex_dependency(str(tmp_path / "missing-codex"))

    assert status.state is DependencyState.MISSING


def test_status_schema_reports_verified_chatgpt(tmp_path):
    status = service(tmp_path).status(refresh=True)
    payload = status.to_dict()

    assert status.state is AuthState.AUTHENTICATED_CHATGPT
    assert status.ready is True
    assert status.dependency.state is DependencyState.COMPATIBLE
    assert payload == {
        "schema_version": AUTH_STATUS_SCHEMA_VERSION,
        "provider": "codex_subscription",
        "state": "authenticated_chatgpt",
        "authenticated": True,
        "ready": True,
        "auth_mode": "chatgpt",
        "plan_type": "plus",
        "workspace": None,
        "dependency": {
            "state": "compatible",
            "executable": sys.executable,
            "version": "1.2.3",
            "minimum_version": "0.100.0",
            "detail": None,
        },
        "checked_at": "2026-08-09T12:00:00Z",
        "error": None,
    }
    json.dumps(payload)


def test_status_hashes_workspace_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "workspace_metadata")
    status = service(tmp_path).status()

    assert status.workspace is not None
    assert status.workspace.name == "Personal"
    assert status.workspace.id_hash
    assert "workspace-secret-id" not in json.dumps(status.to_dict())


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("signed_out", AuthState.SIGNED_OUT),
        ("api_key", AuthState.AUTHENTICATED_API_KEY),
        ("expired", AuthState.EXPIRED_OR_REVOKED),
        ("workspace_denied", AuthState.WORKSPACE_DENIED),
        ("network_failure", AuthState.NETWORK_UNAVAILABLE),
        ("invalid_json", AuthState.UNKNOWN_PROTOCOL_ERROR),
        ("old_cli", AuthState.DEPENDENCY_INCOMPATIBLE),
    ],
)
def test_status_distinguishes_material_states(monkeypatch, tmp_path, mode, expected):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", mode)
    status = service(tmp_path).status()

    assert status.state is expected
    assert status.ready is False


def test_refresh_failure_is_not_collapsed_to_signed_out(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "refresh_failure")
    status = service(tmp_path).status(refresh=True)

    assert status.state is AuthState.REFRESH_FAILED
    assert status.error is not None
    assert status.error.code == ErrorCode.AUTH_REFRESH_FAILED.value
    assert status.error.retryable is True


def test_api_key_mode_explicitly_blocks_subscription_billing(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "api_key")
    status = service(tmp_path).status()

    assert status.authenticated is True
    assert status.ready is False
    assert status.error is not None
    assert status.error.code == ErrorCode.AUTH_BILLING_MODE_MISMATCH.value
    assert status.error.retryable is False
    assert "separate API billing" in status.error.message


def test_browser_login_makes_url_visible_before_waiting(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "signed_out_then_login")
    events: list[tuple[str, object]] = []

    status = service(tmp_path).login(
        LoginMethod.BROWSER,
        on_challenge=lambda challenge: events.append(("challenge", challenge.to_dict())),
        on_progress=lambda progress: events.append(("progress", progress)),
    )

    assert events[0][0] == "challenge"
    challenge = events[0][1]
    assert isinstance(challenge, dict)
    assert challenge["url"] == "https://example.test/browser-login"
    assert challenge["user_code"] is None
    assert events[1] == ("progress", "waiting_for_login")
    assert status.state is AuthState.AUTHENTICATED_CHATGPT


def test_terminal_login_keeps_wait_and_verification_progress_visible(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "signed_out_then_login")
    output = StringIO()

    status = login_interactive(
        service(tmp_path),
        method=LoginMethod.DEVICE_CODE,
        console=Console(file=output, force_terminal=False, color_system=None),
        open_browser=False,
    )

    rendered = output.getvalue()
    assert status.ready is True
    assert "https://example.test/device" in rendered
    assert "ABCD-EFGH" in rendered
    assert "Waiting for sign-in" in rendered
    assert "verifying the ChatGPT account" in rendered


def test_two_stage_login_exposes_explicit_pending_state(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "signed_out_then_login")

    with service(tmp_path).begin_login(LoginMethod.BROWSER) as session:
        pending = session.pending_status()

        assert pending.state is AuthState.LOGIN_PENDING
        assert pending.ready is False
        assert pending.auth_mode == "browser"


def test_device_login_exposes_url_code_expiry_and_cancel_hint(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "signed_out_then_login")
    seen = []

    status = service(tmp_path).login(
        LoginMethod.DEVICE_CODE,
        on_challenge=lambda challenge: seen.append(challenge.to_dict()),
    )

    assert status.ready is True
    assert seen == [
        {
            "schema_version": 1,
            "login_id": "login_fake",
            "method": "device_code",
            "url": "https://example.test/device",
            "user_code": "ABCD-EFGH",
            "expires_in_seconds": 900,
            "waiting": True,
            "cancel_hint": "Press Ctrl+C to cancel",
        }
    ]


def test_login_handles_account_updated_before_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "login_update_first")
    status = service(tmp_path).login(LoginMethod.BROWSER, on_challenge=lambda _value: None)

    assert status.state is AuthState.AUTHENTICATED_CHATGPT


def test_login_failure_is_surfaced_not_suppressed(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "login_failure")

    with pytest.raises(AuthServiceError) as caught:
        service(tmp_path).login(LoginMethod.BROWSER, on_challenge=lambda _value: None)

    assert caught.value.status.ready is False
    assert caught.value.status.error is not None
    assert "callback failed" in caught.value.status.error.message


def test_zero_or_malformed_challenge_cannot_report_success(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "malformed_login")

    with pytest.raises(AuthServiceError) as caught:
        service(tmp_path).login(LoginMethod.BROWSER, on_challenge=lambda _value: None)

    assert caught.value.status.state is AuthState.UNKNOWN_PROTOCOL_ERROR


def test_login_timeout_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "login_timeout")

    with pytest.raises(AuthServiceError) as caught:
        service(tmp_path, timeout=1).login(
            LoginMethod.DEVICE_CODE,
            on_challenge=lambda _value: None,
        )

    assert caught.value.status.state is AuthState.NETWORK_UNAVAILABLE
    assert caught.value.status.error is not None
    assert "1s" in caught.value.status.error.message
    assert "retry" in caught.value.status.error.recovery.lower()


def test_device_expiry_is_distinct_from_network_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "device_expired")

    with pytest.raises(AuthServiceError) as caught:
        service(tmp_path).login(
            LoginMethod.DEVICE_CODE,
            on_challenge=lambda _value: None,
        )

    assert caught.value.status.state is AuthState.EXPIRED_OR_REVOKED
    assert caught.value.status.error is not None
    assert caught.value.status.error.code == ErrorCode.AUTH_EXPIRED_OR_REVOKED.value


def test_zero_completion_but_signed_out_account_is_not_success(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "login_zero_signed_out")

    with pytest.raises(AuthServiceError) as caught:
        service(tmp_path).login(LoginMethod.BROWSER, on_challenge=lambda _value: None)

    assert caught.value.status.state is AuthState.SIGNED_OUT
    assert caught.value.status.ready is False


def test_post_login_account_verification_timeout_is_non_success(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "login_verification_timeout")

    with pytest.raises(AuthServiceError) as caught:
        service(tmp_path, timeout=1).login(
            LoginMethod.BROWSER,
            on_challenge=lambda _value: None,
        )

    assert caught.value.status.state is AuthState.NETWORK_UNAVAILABLE
    assert caught.value.status.ready is False
    assert caught.value.status.error is not None
    assert "1s" in caught.value.status.error.message


def test_nonzero_app_server_exit_is_dependency_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "appserver_nonzero")

    status = service(tmp_path).status()

    assert status.state is AuthState.DEPENDENCY_INCOMPATIBLE
    assert status.ready is False
    assert status.error is not None
    assert status.error.code == ErrorCode.AUTH_DEPENDENCY_INCOMPATIBLE.value


def test_auth_error_json_has_complete_actionable_anatomy(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "network_failure")

    payload = service(tmp_path).status().to_dict()["error"]

    assert payload is not None
    assert payload["code"].startswith("JARN-AUTH-")
    assert payload["summary"]
    assert payload["cause"]
    assert payload["component"] == "authentication"
    assert payload["retryable"] is True
    assert payload["action"]
    assert payload["log_path"]


def test_closing_pending_login_sends_scoped_cancel(monkeypatch, tmp_path):
    events: list[tuple[str, str | None]] = []

    class FakeServer:
        def __init__(self, **_kwargs):
            self.timeout_seconds = 5.0

        def __enter__(self):
            return self

        def start_login(self, *, device_code=False, on_notification=None):
            del device_code, on_notification
            return {
                "loginId": "pending-login",
                "authUrl": "https://example.test/login",
            }

        def cancel_login(self, login_id):
            events.append(("cancel", login_id))

        def close(self):
            events.append(("close", None))

    monkeypatch.setattr("jarn.auth.service.CodexAppServer", FakeServer)
    session = service(tmp_path).begin_login(LoginMethod.BROWSER).open()

    session.close()

    assert events == [("cancel", "pending-login"), ("close", None)]


def test_logout_verifies_signed_out_state(tmp_path):
    status = service(tmp_path).logout()

    assert status.state is AuthState.SIGNED_OUT
    assert status.authenticated is False
    assert status.dependency.state is DependencyState.COMPATIBLE
