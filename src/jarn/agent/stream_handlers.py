"""LangGraph stream chunk handling for the session driver."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from langgraph.types import Overwrite

from jarn.agent.events import Event, EventKind

if TYPE_CHECKING:
    from jarn.agent.session import SessionDriver

# LangChain message ``.type`` values that represent assistant output.
_ASSISTANT_TYPES = {"ai", "AIMessageChunk"}
# Content-block / kwarg shapes various providers use for extended-reasoning text.
_REASONING_TYPES = {"thinking", "reasoning", "reasoning_content"}
# Upper bound on retained tool output for Ctrl+O expand (guards memory).
_MAX_FULL_CHARS = 100_000


def handle_message_chunk(driver: SessionDriver, chunk: Any) -> Event | None:
    msg = chunk[0] if isinstance(chunk, tuple) else chunk
    record_usage(driver, msg)
    mtype = getattr(msg, "type", "")
    # Tool results (ToolMessage) — e.g. a fetched web page — must not be
    # dumped into the chat, but a one-line summary ("3 lines", "12 results")
    # under the tool call mirrors Claude Code's "⎿ result" affordance.
    if mtype == "tool":
        full = _text_of(getattr(msg, "content", "")).strip()
        tool_name = getattr(msg, "name", "") or ""
        data: dict[str, Any] = {"summary": _tool_summary(full, tool_name)}
        tool_call_id = getattr(msg, "tool_call_id", None)
        if tool_call_id:
            data["tool_call_id"] = str(tool_call_id)
        # Keep the full payload (capped) for on-demand expand (Ctrl+O), but
        # only when there is genuinely more to see than the summary line.
        if full and (full.count("\n") or len(full) > 80):
            data["full"] = full[:_MAX_FULL_CHARS]
        return Event(EventKind.TOOL_END, text=getattr(msg, "name", "") or "tool", data=data)
    # Otherwise only stream ASSISTANT text; the model's reply is what the
    # user should see.
    if mtype not in _ASSISTANT_TYPES:
        return None
    content = _text_of(getattr(msg, "content", ""))
    if content:
        return Event(EventKind.TEXT, text=content)
    # No visible answer text in this chunk: surface extended-reasoning text
    # (Anthropic thinking blocks, DeepSeek `reasoning_content`, …) if present.
    reasoning = _reasoning_of(msg)
    if reasoning:
        return Event(EventKind.REASONING, text=reasoning)
    return None


def handle_update_chunk(
    driver: SessionDriver, chunk: dict[str, Any], interrupts: list[Any]
):
    if not isinstance(chunk, dict):
        return
    if "__interrupt__" in chunk:
        for intr in chunk["__interrupt__"]:
            interrupts.append(intr)
        return
    for _node, update in chunk.items():
        if not isinstance(update, dict):
            continue
        messages = update.get("messages", []) or []
        if isinstance(messages, Overwrite):
            messages = messages.value or []
        for msg in messages:
            for call in getattr(msg, "tool_calls", None) or []:
                name = call.get("name", "tool")
                args = call.get("args", {}) or {}
                if name in ("write_file", "edit_file"):
                    driver._last_edit_target = str(
                        args.get("file_path") or args.get("path")
                        or args.get("filename") or ""
                    )
                data: dict[str, Any] = {"args": args}
                call_id = call.get("id")
                if call_id:
                    data["tool_call_id"] = str(call_id)
                yield Event(EventKind.TOOL_START, text=name, data=data)


def record_usage(driver: SessionDriver, msg: Any) -> None:
    usage = getattr(msg, "usage_metadata", None)
    if not usage:
        return
    # LangChain reports prompt-cache tokens under ``input_token_details``
    # (``cache_read`` / ``cache_creation``); absent for providers/turns
    # without caching, in which case both default to 0.
    details = usage.get("input_token_details") or {}
    cumulative = (
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        int(details.get("cache_read", 0)),
        int(details.get("cache_creation", 0)),
    )
    model_ref = resolve_model_ref(driver, msg)
    usage_key = (driver.thread_id, model_ref)
    prev = driver._last_usage_totals.get(usage_key)
    is_continuation = False
    if prev is not None:
        monotonic = cumulative[0] >= prev[0] and cumulative[1] >= prev[1]
        if monotonic:
            delta = tuple(max(0, c - p) for c, p in zip(cumulative, prev, strict=True))
            if not any(delta):
                return
            input_tokens, output_tokens, cache_read, cache_creation = delta
            is_continuation = True
        else:
            # A new API call within the same turn (per-call totals, not cumulative).
            input_tokens, output_tokens, cache_read, cache_creation = cumulative
    else:
        input_tokens, output_tokens, cache_read, cache_creation = cumulative
    driver._last_usage_totals[usage_key] = cumulative

    # Attribute to main model: ctx% gauge only updates for the main model's prompt.
    # NOTE: a subagent that shares the main model's id cannot be distinguished here
    # (resolve_model_ref will return main_model_ref for both). That case is documented
    # as a known limitation — the gauge may be inflated by same-model subagent prompts.
    # Distinguishable case (different model id) is fully handled and tested.
    is_main = model_ref == driver.main_model_ref

    tools = _tool_names(msg)
    driver.tracker.record(
        model_ref,
        input_tokens,
        output_tokens,
        tools=tools if len(tools) > 1 else None,
        tool=tools[0] if len(tools) == 1 else None,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        increment_call=not is_continuation,
        is_main=is_main,
    )


def resolve_model_ref(driver: SessionDriver, msg: Any) -> str:
    """Attribute a streamed chunk to the model that actually produced it.

    The provider stamps the model on the message itself —
    ``response_metadata['model_name']`` (OpenAI-compatible, incl. OpenRouter)
    or ``['model']`` (Anthropic). We canonicalize that raw name to one of the
    refs we know about (``known_model_refs``: main + subagents + summarizer) so
    the per-model bucket uses our pricing-resolvable ref and the main model
    keeps a single stable label. When the message carries no model (e.g. an
    early streaming chunk) we fall back to the main model — preserving today's
    single-model behavior exactly. Reading the reported model is reliable;
    guessing from the subgraph namespace was not (it omits the subagent name).
    """
    meta = getattr(msg, "response_metadata", None)
    name = ""
    if isinstance(meta, dict):
        name = str(meta.get("model_name") or meta.get("model") or "")
    if not name:
        return driver.main_model_ref
    # Canonicalize to the most specific known ref. Substring either direction so
    # "claude-opus-4-8" <-> "anthropic/claude-opus-4-8" both resolve.
    best: str | None = None
    for ref in driver.known_model_refs:
        if ref and (name in ref or ref in name) and (best is None or len(ref) > len(best)):
            best = ref
    # No known ref matched: record under the provider's raw name (pricing still
    # substring-resolves it) rather than mislabeling it as the main model.
    return best if best is not None else name


def _unpack_stream_item(item: Any) -> tuple[Any, str | None, Any]:
    """Normalize a LangGraph astream item to ``(namespace, mode, chunk)``.

    With ``subgraphs=True`` items are ``(namespace, mode, chunk)``; without it
    they are ``(mode, chunk)`` (namespace then defaults to ``()``). The namespace
    is a *tuple* path of subgraph node names (e.g. ``("tools:<id>", …)``) — it is
    used to attribute usage to the subagent model that produced the chunk; subagent
    *output* is still shown just like top-level output.
    """
    if isinstance(item, tuple):
        if len(item) == 3:
            return item[0], item[1], item[2]
        if len(item) == 2:
            return (), item[0], item[1]
    return (), None, None


def _reasoning_of(msg: Any) -> str:
    """Extract extended-reasoning text from a chunk, across provider shapes."""
    parts: list[str] = []
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in _REASONING_TYPES:
                parts.append(
                    block.get("thinking")
                    or block.get("reasoning")
                    or block.get("reasoning_content")
                    or block.get("text")
                    or ""
                )
    extra = getattr(msg, "additional_kwargs", None) or {}
    if isinstance(extra, dict) and extra.get("reasoning_content"):
        parts.append(str(extra["reasoning_content"]))
    return "".join(parts)


def _tool_names(msg: Any) -> list[str]:
    """All tools a model call requested, for per-tool cost attribution."""
    names: list[str] = []
    seen: set[str] = set()
    for attr in ("tool_calls", "tool_call_chunks"):
        for call in getattr(msg, attr, None) or []:
            name = call.get("name") if isinstance(call, dict) else None
            if name and name not in seen:
                seen.add(str(name))
                names.append(str(name))
    return names


def _first_tool_name(msg: Any) -> str | None:
    """The first tool a model call requested (compat shim)."""
    names = _tool_names(msg)
    return names[0] if names else None


#: Substrings / exception-name fragments that mark a provider error as worth a
#: model fallback (transient or capacity-related, not a logic bug).
_RETRYABLE_NAME_HINTS = ("timeout", "connecterror", "connectionerror", "ratelimit",
                         "overloaded", "serviceunavailable", "apierror", "internalserver")
# Narrow phrases only — a bare "connection"/"timeout" can appear in unrelated
# tool-result strings, which would trigger a needless model rotation.
_RETRYABLE_MSG_HINTS = ("rate limit", "rate-limit", "overloaded",
                        "temporarily unavailable", "service unavailable",
                        "connection reset", "connection refused", "connection aborted",
                        "connection error", "read timed out", "request timed out")


def _is_retryable_error(exc: BaseException) -> bool:
    """Heuristic: is this a transient/capacity provider error worth falling back?

    Providers raise wildly different exception types, so we match on the type
    name and message rather than a fixed exception class. A numeric ``status_code``
    in the 429/5xx family also counts.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    name = type(exc).__name__.lower()
    if any(h in name for h in _RETRYABLE_NAME_HINTS):
        return True
    msg = str(exc).lower()
    return any(h in msg for h in _RETRYABLE_MSG_HINTS)


#: Exception-name fragments and message phrases that mark a provider failure as an
#: authentication/authorization rejection — an invalid/expired/missing API key.
#: Rotating to another model won't fix this, so it is handled separately from the
#: retryable heuristic above (see ``_is_retryable_error``).
_AUTH_NAME_HINTS = ("authenticationerror", "permissiondenied", "unauthorized")
_AUTH_MSG_HINTS = ("invalid x-api-key", "invalid api key", "invalid_api_key",
                   "incorrect api key", "authentication_error", "authentication error",
                   "unauthorized", "no auth credentials", "missing api key",
                   "expired api key", "invalid bearer token", "permission denied")


def _is_auth_error(exc: BaseException) -> bool:
    """Heuristic: is this a 401/403 auth rejection (bad/expired/missing key)?

    Matches on a numeric ``status_code`` of 401/403, the exception type name, or
    known message phrases. Kept deliberately narrow so a generic "permission"
    string in an unrelated tool result doesn't get misclassified.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (401, 403):
        return True
    name = type(exc).__name__.lower()
    if any(h in name for h in _AUTH_NAME_HINTS):
        return True
    msg = str(exc).lower()
    if "401" in msg or "403" in msg:
        return True
    return any(h in msg for h in _AUTH_MSG_HINTS)


def _provider_of_ref(ref: str) -> str:
    """Best-effort provider/profile name from a model ref (e.g. ``anthropic`` from
    ``anthropic/claude-opus-4-8``). Returns ``""`` when it can't be determined so
    the caller can fall back to a generic phrasing."""
    if not ref or ref == "unknown":
        return ""
    return ref.split("/", 1)[0] if "/" in ref else ""


def _web_search_summary(content: str) -> str:
    """Richer one-line summary for web_search results: count + top source hosts."""
    # Count result entries (each starts with "- <Title>") by counting "  https?://" lines.
    urls = re.findall(r"^\s{2}(https?://[^\s]+)", content, re.MULTILINE)
    if not urls:
        # Fallback: no URLs found — use generic summary.
        return _tool_summary(content)
    count = len(urls)
    # Build a deduplicated list of hosts in order of first appearance.
    seen: dict[str, None] = {}
    for u in urls:
        # .hostname (not .netloc) so a credential-bearing URL never leaks its
        # user:pass@ userinfo (or :port) into the inline summary.
        host = urlparse(u).hostname or urlparse(u).netloc or u
        # Strip www. prefix for compactness.
        if host.startswith("www."):
            host = host[4:]
        seen[host] = None
    hosts = list(seen.keys())
    # Show up to 3 hosts; append "…" when there are more.
    shown = hosts[:3]
    suffix = ", …" if len(hosts) > 3 else ""
    hosts_str = ", ".join(shown) + suffix
    return f"🔍 {count} result{'s' if count != 1 else ''} · {hosts_str}"


def _tool_summary(content: str, tool_name: str = "") -> str:
    """A compact one-line summary of a tool result (never the full payload)."""
    if tool_name == "web_search":
        return _web_search_summary(content)
    txt = content.strip()
    if not txt:
        return "(no output)"
    lines = txt.count("\n") + 1
    if lines > 1:
        return f"{lines} lines"
    return txt if len(txt) <= 80 else txt[:79] + "…"


def _text_of(content: Any) -> str:
    """Flatten LangChain message content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
        return "".join(out)
    return ""
