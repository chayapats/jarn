"""Token counting and budget truncation for memory/wiki/context injection."""

from __future__ import annotations

import os
import threading
from typing import Any

from jarn.config.paths import global_home

_CHARS_PER_TOKEN = 4
_ENCODER_LOAD_TIMEOUT = 3.0
_ENCODER_LOCK = threading.Lock()
_ENCODER_LOAD_ATTEMPTED = False
_ENCODER: Any | None = None


def _load_encoder(*, timeout: float | None = None) -> Any | None:
    """Load tiktoken once, returning ``None`` after a bounded wait or failure.

    Tiktoken downloads its vocabulary on a cold cache without applying a request
    timeout.  Run that load in a daemon thread so dropped egress cannot stall the
    process, and cache failures as well as successes so callers do not repeatedly
    retry the same download.
    """
    global _ENCODER, _ENCODER_LOAD_ATTEMPTED

    if _ENCODER_LOAD_ATTEMPTED:
        return _ENCODER

    if timeout is None:
        timeout = _ENCODER_LOAD_TIMEOUT

    with _ENCODER_LOCK:
        if _ENCODER_LOAD_ATTEMPTED:
            return _ENCODER

        cache_dir = global_home() / "cache" / "tiktoken"
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache_dir))
        result: dict[str, Any] = {}

        def _load() -> None:
            try:
                import tiktoken

                result["encoder"] = tiktoken.get_encoding("cl100k_base")
            except Exception:  # noqa: BLE001
                # Token counting is an optimization; the deterministic fallback
                # below is preferable to failing session startup.
                pass

        worker = threading.Thread(target=_load, name="jarn-tiktoken-load", daemon=True)
        worker.start()
        worker.join(max(0.0, timeout))

        _ENCODER = result.get("encoder") if not worker.is_alive() else None
        _ENCODER_LOAD_ATTEMPTED = True
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
