"""Token counting and budget truncation for memory/wiki/context injection."""

from __future__ import annotations

_CHARS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    """Count tokens in *text*, falling back to len/4 if tiktoken fails."""
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001
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
