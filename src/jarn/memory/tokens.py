"""Token counting and budget truncation for memory/wiki/context injection."""

from __future__ import annotations

import os
import threading
from typing import Any

from jarn.config.paths import global_home

_CHARS_PER_TOKEN = 4
_ENCODER_LOAD_TIMEOUT = 3.0
_ENCODER_LOCK = threading.Lock()
_ENCODER: Any | None = None
_ENCODER_FAILED = False
_ENCODER_WAITED = False
_ENCODER_WORKER: threading.Thread | None = None
_ENCODER_RESULT: dict[str, Any] = {}


def _configure_tiktoken_cache() -> None:
    """Point tiktoken at a persistent cache, but only when we have a usable one.

    Two things make this narrower than it looks.

    Naming a cache directory flips tiktoken's ``user_specified_cache``, and that
    branch **re-raises** cache-write ``OSError``s its own tempdir default
    swallows. On a host where ``$JARN_HOME`` is not writable (read-only HOME, a
    container running as a random uid) claiming the directory therefore turns a
    working fallback into no encoder at all — and ``count_tokens`` silently
    switches to ``len // 4``, which is off by roughly 2x on prose. So the
    directory has to be creatable before we claim it.

    And tiktoken checks ``TIKTOKEN_CACHE_DIR`` before ``DATA_GYM_CACHE_DIR``, so
    setting ours would silently shadow a cache the operator pre-warmed.
    """
    if os.environ.get("TIKTOKEN_CACHE_DIR") or os.environ.get("DATA_GYM_CACHE_DIR"):
        return
    try:
        cache_dir = global_home() / "cache" / "tiktoken"
        cache_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError):
        # RuntimeError: no home at all (unset $HOME, uid with no passwd entry).
        # Either way, leave tiktoken on the default that tolerates write failure.
        return
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)


def _load_encoder(*, timeout: float | None = None) -> Any | None:
    """Load tiktoken once, returning ``None`` after a bounded wait or failure.

    Tiktoken downloads its vocabulary on a cold cache without applying a request
    timeout. Run that load in a daemon thread so dropped egress cannot stall the
    process.

    A load that is still running is *not* recorded as a failure. A cold download
    routinely outlasts the default budget, and treating that as terminal pinned
    the whole session to the ``len // 4`` fallback and made the 30 s
    ``warm_tokenizer_cache`` a no-op, since it short-circuited on the earlier
    verdict. Only the blocking wait is spent once — later callers check without
    paying it again, so a slow download cannot stall every count in a truncation
    loop, and whoever calls after it lands picks up the encoder.
    """
    global _ENCODER, _ENCODER_FAILED, _ENCODER_WAITED, _ENCODER_WORKER

    if _ENCODER is not None or _ENCODER_FAILED:
        return _ENCODER

    explicit_budget = timeout

    with _ENCODER_LOCK:
        if _ENCODER is not None or _ENCODER_FAILED:
            return _ENCODER

        if _ENCODER_WORKER is None:
            _configure_tiktoken_cache()

            def _load() -> None:
                try:
                    import tiktoken

                    _ENCODER_RESULT["encoder"] = tiktoken.get_encoding("cl100k_base")
                except Exception:  # noqa: BLE001
                    # Token counting is an optimization; the deterministic
                    # fallback below beats failing session startup.
                    _ENCODER_RESULT["failed"] = True

            _ENCODER_WORKER = threading.Thread(
                target=_load, name="jarn-tiktoken-load", daemon=True
            )
            _ENCODER_WORKER.start()

        if explicit_budget is not None:
            budget = explicit_budget
        else:
            budget = 0.0 if _ENCODER_WAITED else _ENCODER_LOAD_TIMEOUT
            _ENCODER_WAITED = True

        _ENCODER_WORKER.join(max(0.0, budget))
        if _ENCODER_WORKER.is_alive():
            return None  # still downloading — ask again later, don't give up

        _ENCODER = _ENCODER_RESULT.get("encoder")
        _ENCODER_FAILED = _ENCODER is None
        return _ENCODER


def warm_tokenizer_cache(*, timeout: float = 30.0) -> bool:
    """Try to populate the persistent tokenizer cache during interactive setup."""
    return _load_encoder(timeout=timeout) is not None


def count_tokens(text: str) -> int:
    """Count tokens in *text*, falling back to len/4 if tiktoken fails."""
    if not text:
        return 0
    enc = _load_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // _CHARS_PER_TOKEN)


def truncate_to_token_budget(text: str, budget: int) -> str:
    """Truncate *text* to fit *budget* tokens, appending a visible notice."""
    if budget <= 0 or not text:
        return text
    total = count_tokens(text)
    if total <= budget:
        return text

    notice_template = "\n\n(truncated {n} tokens)\n"
    # Reserve space for the notice (worst case: large n).
    notice_reserve = count_tokens(notice_template.format(n=total))
    target = max(1, budget - notice_reserve)

    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [text]

    kept: list[str] = []
    used = 0
    for line in lines:
        cost = count_tokens(line)
        if used + cost > target:
            break
        kept.append(line)
        used += cost

    body = "".join(kept).rstrip()
    removed = total - count_tokens(body)
    if removed <= 0:
        # Single huge line — hard-cut by characters.
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count_tokens(text[:mid]) <= target:
                lo = mid
            else:
                hi = mid - 1
        body = text[:lo].rstrip()
        removed = total - count_tokens(body)

    notice = notice_template.format(n=removed)
    return body + notice
