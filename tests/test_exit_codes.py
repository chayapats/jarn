"""Public CLI exit-code and structured-failure contract."""

from __future__ import annotations

import asyncio

from jarn.errors import ErrorCode, JarnUserError, error_detail
from jarn.exit_codes import (
    EXIT_AUTH,
    EXIT_BUDGET_EXCEEDED,
    EXIT_CANCELLED,
    EXIT_INTERNAL,
    EXIT_MODEL_UNAVAILABLE,
    EXIT_NETWORK_PROVIDER,
    EXIT_PERMISSION_DENIED,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_UPDATE_FAILED,
    EXIT_USAGE_CONFIG,
    EXIT_VERIFICATION_FAILED,
)
from jarn.headless import (
    HeadlessFailure,
    _classify_exception,
    _error_from_event,
    _public_failure_details,
)


def test_required_exit_codes_are_stable_and_distinct() -> None:
    required = {
        EXIT_SUCCESS,
        EXIT_USAGE_CONFIG,
        EXIT_AUTH,
        EXIT_MODEL_UNAVAILABLE,
        EXIT_PERMISSION_DENIED,
        EXIT_NETWORK_PROVIDER,
        EXIT_CANCELLED,
        EXIT_UPDATE_FAILED,
        EXIT_INTERNAL,
    }
    assert len(required) == 9
    assert EXIT_SUCCESS == 0
    assert EXIT_TIMEOUT == 124
    assert EXIT_CANCELLED == 130
    assert EXIT_BUDGET_EXCEEDED not in required
    assert EXIT_VERIFICATION_FAILED not in required


def test_typed_user_errors_map_to_exit_taxonomy() -> None:
    cases = (
        (ErrorCode.CONFIG_INVALID_SCHEMA, EXIT_USAGE_CONFIG, "config"),
        (ErrorCode.AUTH_FAILED, EXIT_AUTH, "auth"),
        (ErrorCode.MODEL_UNAVAILABLE, EXIT_MODEL_UNAVAILABLE, "model"),
        (ErrorCode.PERMISSION_DENIED, EXIT_PERMISSION_DENIED, "permission"),
        (ErrorCode.NETWORK_FAILED, EXIT_NETWORK_PROVIDER, "network"),
        (ErrorCode.INTERNAL, EXIT_INTERNAL, "internal"),
    )
    for code, expected_exit, expected_kind in cases:
        exc = JarnUserError(
            error_detail(
                code,
                "safe summary",
                cause="safe cause",
                component="test",
                retryable=False,
                action="retry deliberately",
            )
        )
        classified = _classify_exception(exc)
        assert classified.exit_code == expected_exit
        assert classified.kind == expected_kind
        assert classified.details["code"] == code.value


def test_provider_events_distinguish_auth_model_network_and_timeout() -> None:
    assert _error_from_event("401 rejected", {"auth": True}).exit_code == EXIT_AUTH
    assert (
        _error_from_event("model not found", {"retryable": False}).exit_code
        == EXIT_MODEL_UNAVAILABLE
    )
    assert (
        _error_from_event("provider disconnected", {"retryable": True}).exit_code
        == EXIT_NETWORK_PROVIDER
    )
    assert (
        _error_from_event("request timed out", {"retryable": True}).exit_code
        == EXIT_TIMEOUT
    )


def test_cancellation_is_not_an_internal_error() -> None:
    assert _classify_exception(KeyboardInterrupt()).exit_code == EXIT_CANCELLED
    assert _classify_exception(asyncio.CancelledError()).exit_code == EXIT_CANCELLED


def test_machine_failure_has_complete_actionable_anatomy() -> None:
    payload = _public_failure_details(
        HeadlessFailure("auth", "not signed in", exit_code=EXIT_AUTH)
    )
    assert payload["code"] == ErrorCode.AUTH_FAILED.value
    assert payload["summary"]
    assert payload["cause"]
    assert payload["component"] == "headless"
    assert payload["retryable"] is False
    assert "jarn auth" in payload["action"]
    assert payload["log_path"]


def test_unknown_headless_failure_points_to_real_diagnostic_commands() -> None:
    payload = _public_failure_details(HeadlessFailure("mystery", "unexpected"))
    assert "doctor --report" in payload["action"]
    assert "--verbose" not in payload["action"]
