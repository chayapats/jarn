"""Tests for typed error classification (T-1-8).

Covers the matrix from the task brief: status codes, timeouts, cause-chain walk,
heuristic fallback, and auth-vs-retryable precedence.

httpx is a jarn dependency so its real classes are used; anthropic/openai are
import-guarded (not guaranteed in all envs) and feature-detected by attribute.
"""
from __future__ import annotations

import asyncio

import httpx

from jarn.agent.stream_handlers import classify_error  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _http_exc(status: int) -> httpx.HTTPStatusError:
    """Build a real httpx.HTTPStatusError with the given status code."""
    req = httpx.Request("GET", "https://api.example.com/v1/messages")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


def _wrapped(outer: BaseException, cause: BaseException) -> BaseException:
    """Simulate ``raise outer from cause``, returning outer with __cause__ set."""
    try:
        raise outer from cause
    except type(outer) as exc:
        return exc


# ---------------------------------------------------------------------------
# test_status_codes — direct httpx.HTTPStatusError with various status codes
# ---------------------------------------------------------------------------

class TestStatusCodes:
    def test_429_is_retryable(self) -> None:
        r = classify_error(_http_exc(429))
        assert r["retryable"] is True
        assert r["auth"] is False
        assert r["classified_by"] == "type"

    def test_401_is_auth(self) -> None:
        r = classify_error(_http_exc(401))
        assert r["auth"] is True
        assert r["retryable"] is False
        assert r["classified_by"] == "type"

    def test_403_is_auth(self) -> None:
        r = classify_error(_http_exc(403))
        assert r["auth"] is True
        assert r["retryable"] is False
        assert r["classified_by"] == "type"

    def test_500_is_retryable(self) -> None:
        r = classify_error(_http_exc(500))
        assert r["retryable"] is True
        assert r["auth"] is False
        assert r["classified_by"] == "type"

    def test_529_is_retryable(self) -> None:
        """529 = Anthropic 'overloaded' status."""
        r = classify_error(_http_exc(529))
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_503_is_retryable(self) -> None:
        r = classify_error(_http_exc(503))
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_408_is_retryable(self) -> None:
        """408 Request Timeout is retryable."""
        r = classify_error(_http_exc(408))
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_400_is_non_retryable(self) -> None:
        r = classify_error(_http_exc(400))
        assert r["retryable"] is False
        assert r["auth"] is False
        assert r["classified_by"] == "type"

    def test_status_code_attr_on_plain_exception(self) -> None:
        """Any exception with a numeric status_code attribute is classified typed."""
        class _SDKError(Exception):
            status_code = 429

        r = classify_error(_SDKError("rate limited"))
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_auth_status_code_attr(self) -> None:
        class _AuthError(Exception):
            status_code = 401

        r = classify_error(_AuthError("unauthorized"))
        assert r["auth"] is True
        assert r["retryable"] is False
        assert r["classified_by"] == "type"


# ---------------------------------------------------------------------------
# test_timeouts — httpx + asyncio + built-in
# ---------------------------------------------------------------------------

class TestTimeouts:
    def test_httpx_timeout_exception(self) -> None:
        exc = httpx.TimeoutException("timed out")
        r = classify_error(exc)
        assert r["retryable"] is True
        assert r["auth"] is False
        assert r["classified_by"] == "type"

    def test_httpx_read_timeout(self) -> None:
        """ReadTimeout is a subclass of TimeoutException."""
        req = httpx.Request("GET", "https://api.example.com/v1/messages")
        exc = httpx.ReadTimeout("read timed out", request=req)
        r = classify_error(exc)
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_asyncio_timeout_error(self) -> None:
        # asyncio.TimeoutError is an alias for TimeoutError in Python 3.11+
        exc = asyncio.TimeoutError()  # noqa: UP041
        r = classify_error(exc)
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_builtin_timeout_error(self) -> None:
        exc = TimeoutError("operation timed out")
        r = classify_error(exc)
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_connection_error_is_retryable(self) -> None:
        exc = ConnectionError("connection refused")
        r = classify_error(exc)
        assert r["retryable"] is True
        assert r["classified_by"] == "type"


# ---------------------------------------------------------------------------
# test_cause_chain — __cause__ walk finds the typed exception
# ---------------------------------------------------------------------------

class TestCauseChain:
    def test_status_code_on_direct_cause(self) -> None:
        """raise ValueError from httpx.HTTPStatusError(429): classify via __cause__."""
        cause = _http_exc(429)
        outer = _wrapped(ValueError("model failed"), cause)
        r = classify_error(outer)
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_auth_on_direct_cause(self) -> None:
        cause = _http_exc(401)
        outer = _wrapped(RuntimeError("provider error"), cause)
        r = classify_error(outer)
        assert r["auth"] is True
        assert r["retryable"] is False
        assert r["classified_by"] == "type"

    def test_deep_chain_depth_2(self) -> None:
        """Chain depth > 1: outer → middle → typed cause."""
        typed = _http_exc(503)
        middle = _wrapped(RuntimeError("layer 2"), typed)
        outer = _wrapped(Exception("layer 1"), middle)
        r = classify_error(outer)
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_chain_stops_at_first_conclusive(self) -> None:
        """If outer is typed (auth) and cause is also typed (retryable), outer wins."""
        retryable_cause = _http_exc(429)
        auth_outer = _wrapped(_http_exc(401), retryable_cause)
        r = classify_error(auth_outer)
        # The walk finds auth_outer first (it has status_code=401)
        assert r["auth"] is True
        assert r["retryable"] is False
        assert r["classified_by"] == "type"

    def test_inconclusive_outer_resolves_via_cause(self) -> None:
        """An unrecognised outer exception falls through to its cause."""
        cause = httpx.TimeoutException("timed out")
        # RuntimeError is not typed, so we should walk to cause
        outer = _wrapped(RuntimeError("something failed"), cause)
        r = classify_error(outer)
        assert r["retryable"] is True
        assert r["classified_by"] == "type"

    def test_depth_limit_respected(self) -> None:
        """Chain longer than 5 levels: exceptions beyond depth 5 are ignored."""
        # Build a chain 7 levels deep where only level 6 would be retryable
        typed = _http_exc(429)
        exc: BaseException = typed
        for _ in range(6):
            exc = _wrapped(ValueError("wrap"), exc)
        # depth 5 from the outer: outer(1) → wrap(2) → wrap(3) → wrap(4) →
        # wrap(5) → ... the typed exc is 7 levels down, past depth limit
        r = classify_error(exc)
        # Should NOT find the typed cause; falls to heuristic
        assert r["classified_by"] == "heuristic"


# ---------------------------------------------------------------------------
# test_heuristic_fallback_still_works
# ---------------------------------------------------------------------------

class TestHeuristicFallback:
    def test_unknown_value_error_non_retryable(self) -> None:
        r = classify_error(ValueError("unknown model ref"))
        assert r["retryable"] is False
        assert r["auth"] is False
        assert r["classified_by"] == "heuristic"

    def test_message_rate_limit_retryable(self) -> None:
        r = classify_error(Exception("Rate limit exceeded (429)"))
        assert r["retryable"] is True
        assert r["classified_by"] == "heuristic"

    def test_message_overloaded_retryable(self) -> None:
        r = classify_error(Exception("service overloaded, retry later"))
        assert r["retryable"] is True
        assert r["classified_by"] == "heuristic"

    def test_message_auth_error_retryable_false(self) -> None:
        raw = ("Error code: 401 - {'type': 'error', 'error': {'type': "
               "'authentication_error', 'message': 'invalid x-api-key'}}")
        r = classify_error(Exception(raw))
        assert r["auth"] is True
        assert r["retryable"] is False
        assert r["classified_by"] == "heuristic"

    def test_connection_reset_in_message(self) -> None:
        """Plain Exception (not ConnectionError) with a connection reset message."""
        r = classify_error(Exception("upstream connection reset by peer"))
        assert r["retryable"] is True
        assert r["classified_by"] == "heuristic"

    def test_never_raises(self) -> None:
        """classify_error must not raise on weird / incomplete exceptions."""
        class _Weird(Exception):
            # status_code property raises on access
            @property
            def status_code(self):
                raise RuntimeError("boom")

        # Should not propagate
        r = classify_error(_Weird("odd"))
        assert isinstance(r["retryable"], bool)
        assert isinstance(r["auth"], bool)


# ---------------------------------------------------------------------------
# test_auth_vs_retryable_precedence
# ---------------------------------------------------------------------------

class TestAuthVsRetryablePrecedence:
    def test_typed_auth_wins_over_heuristic_retryable_message(self) -> None:
        """When the typed path finds auth, heuristic retryable message is irrelevant."""
        # A 401 HTTPStatusError whose stringification also contains "rate limit"
        req = httpx.Request("GET", "https://x.com")
        resp = httpx.Response(401, request=req)
        exc = httpx.HTTPStatusError(
            "rate limit and auth error 401 rate-limit", request=req, response=resp
        )
        r = classify_error(exc)
        assert r["auth"] is True
        assert r["retryable"] is False
        assert r["classified_by"] == "type"

    def test_typed_takes_precedence_over_heuristic_when_both_could_match(self) -> None:
        """An exc with status_code=429 (typed retryable) and auth-like message."""
        class _Mixed(Exception):
            status_code = 429

        r = classify_error(_Mixed("invalid api key, rate limited"))
        assert r["retryable"] is True
        assert r["auth"] is False
        assert r["classified_by"] == "type"

    def test_heuristic_auth_not_retryable(self) -> None:
        """Heuristic auth classification implies retryable=False."""
        r = classify_error(Exception("401 Unauthorized"))
        assert r["auth"] is True
        assert r["classified_by"] == "heuristic"


# ---------------------------------------------------------------------------
# classified_by key presence
# ---------------------------------------------------------------------------

class TestClassifiedByKey:
    def test_key_present_for_typed(self) -> None:
        r = classify_error(_http_exc(429))
        assert "classified_by" in r

    def test_key_present_for_heuristic(self) -> None:
        r = classify_error(ValueError("nope"))
        assert "classified_by" in r
        assert r["classified_by"] == "heuristic"
