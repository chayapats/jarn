"""LangGraph stream chunk handling for the session driver."""

from __future__ import annotations

import asyncio
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


def _tag_agent(ev: Event | None, agent: str | None) -> Event | None:
    """Stamp ``data['agent']`` when *ev* flows from a correlated subagent namespace.

    Display-only: the front-end uses this to label subagent-originated lines. Main-
    graph events (``agent is None``) are left untouched.
    """
    if ev is not None and agent:
        ev.data["agent"] = agent
    return ev


def _agent_for_namespace(driver: SessionDriver, namespace: Any) -> str | None:
    """Correlate a ``subgraphs=True`` *namespace* to the subagent name behind it.

    A delegated subagent's events arrive under a namespace tuple whose first element
    is ``"tools:<task_id>"`` — a LangGraph checkpoint task id (an opaque UUID-shaped
    hash of the parent checkpoint id + the ``"tools"`` node + step + Send index). It
    is NOT the ``task`` tool_call_id and does not embed it, so the id cannot be mapped
    to a name on its own. The subagent NAME lives only in the ``task`` tool args,
    recorded (in launch order) into ``driver._subagent_pending`` at TOOL_START. Each
    newly-seen namespace is therefore bound to the next pending name on a first-
    appearance (FIFO) basis and remembered (``driver._ns_agent``) for the rest of the
    turn.

    Returns ``None`` for the main graph (namespace ``()``) — those are never tagged —
    and for any subgraph whose ``task`` launch was not observed (no name to attribute).
    Keyed on the *first* namespace element so a subagent's own nested output is
    attributed to that top-level subagent.
    """
    if not namespace:
        return None
    key = namespace[0] if isinstance(namespace, (tuple, list)) else namespace
    bound = driver._ns_agent.get(key)
    if bound is not None:
        return bound
    if driver._subagent_pending:
        name = driver._subagent_pending.pop(0)
        driver._ns_agent[key] = name
        return name
    return None


def handle_message_chunk(
    driver: SessionDriver, chunk: Any, namespace: Any = ()
) -> Event | None:
    msg = chunk[0] if isinstance(chunk, tuple) else chunk
    record_usage(driver, msg)
    agent = _agent_for_namespace(driver, namespace)
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
        return _tag_agent(
            Event(EventKind.TOOL_END, text=getattr(msg, "name", "") or "tool", data=data),
            agent,
        )
    # Otherwise only stream ASSISTANT text; the model's reply is what the
    # user should see.
    if mtype not in _ASSISTANT_TYPES:
        return None
    content = _text_of(getattr(msg, "content", ""))
    if content:
        return _tag_agent(Event(EventKind.TEXT, text=content), agent)
    # No visible answer text in this chunk: surface extended-reasoning text
    # (Anthropic thinking blocks, DeepSeek `reasoning_content`, …) if present.
    reasoning = _reasoning_of(msg)
    if reasoning:
        return _tag_agent(Event(EventKind.REASONING, text=reasoning), agent)
    return None


def chunk_has_ai_message(chunk: Any) -> bool:
    """Whether an ``updates`` chunk is a MODEL super-step (carries an ``AIMessage``).

    Used by the mid-turn steering gate (T-4-6): a steer may only be injected at a
    model-step boundary, because ``aclose()`` rolls the in-flight super-step back to
    the last durable checkpoint. At a model step that rollback lands on the prior
    round's ``ToolMessage``s (or the user message) — a SETTLED point where appending
    a steer cannot strand a ``tool_use``. At a tool step the rollback would land on a
    pending ``AIMessage(tool_calls)`` (unsettled), so those boundaries are skipped.
    ``__interrupt__`` / metadata chunks carry no ``AIMessage`` and return ``False``."""
    if not isinstance(chunk, dict) or "__interrupt__" in chunk:
        return False
    for _node, update in chunk.items():
        if not isinstance(update, dict):
            continue
        messages = update.get("messages", []) or []
        if isinstance(messages, Overwrite):
            messages = messages.value or []
        for msg in messages:
            if getattr(msg, "type", "") in _ASSISTANT_TYPES:
                return True
    return False


def handle_update_chunk(
    driver: SessionDriver, chunk: dict[str, Any], interrupts: list[Any],
    namespace: Any = (),
):
    if not isinstance(chunk, dict):
        return
    if "__interrupt__" in chunk:
        for intr in chunk["__interrupt__"]:
            interrupts.append(intr)
        return
    agent = _agent_for_namespace(driver, namespace)
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
                # A ``task`` launch names the subagent it spawns (in its args); record
                # that name — in call order — so the subagent's later subgraph events
                # can be correlated back to it (see _agent_for_namespace).
                # Only the main graph (namespace == ()) launches top-level subagents;
                # nested task launches from inside a subgraph must not pollute the FIFO.
                if name == "task" and not namespace:
                    sub = str(
                        args.get("subagent_type") or args.get("subagent")
                        or args.get("name") or ""
                    ).strip()
                    if sub:
                        # Dedup by call id: stream_mode=["messages","updates"] +
                        # subgraphs=True can re-emit the same TOOL_START chunk;
                        # a duplicate append would shift the FIFO and mislabel
                        # later subagents. Fall back to appending when call has no id
                        # (shouldn't happen for a real tool call, but be safe).
                        call_id = call.get("id")
                        if call_id is None or call_id not in driver._subagent_seen_calls:
                            driver._subagent_pending.append(sub)
                            if call_id is not None:
                                driver._subagent_seen_calls.add(call_id)
                if name in ("write_file", "edit_file"):
                    driver._last_edit_target = str(
                        args.get("file_path") or args.get("path")
                        or args.get("filename") or ""
                    )
                data: dict[str, Any] = {"args": args}
                call_id = call.get("id")
                if call_id:
                    data["tool_call_id"] = str(call_id)
                if agent:
                    data["agent"] = agent
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


# ---------------------------------------------------------------------------
# Typed error classification (T-1-8)
# ---------------------------------------------------------------------------

#: Substrings / exception-name fragments that mark a provider error as worth a
#: model fallback (transient or capacity-related, not a logic bug).
#: Provider-specific class names without a typed equivalent remain here as the
#: last-resort safety net.  The status_code branch that was previously in
#: ``_is_retryable_error`` is now handled in the typed tier of ``classify_error``.
_RETRYABLE_NAME_HINTS = ("timeout", "connecterror", "connectionerror", "ratelimit",
                         "overloaded", "serviceunavailable", "apierror", "internalserver")
# Narrow phrases only — a bare "connection"/"timeout" can appear in unrelated
# tool-result strings, which would trigger a needless model rotation.
_RETRYABLE_MSG_HINTS = ("rate limit", "rate-limit", "overloaded",
                        "temporarily unavailable", "service unavailable",
                        "connection reset", "connection refused", "connection aborted",
                        "connection error", "read timed out", "request timed out")

#: Exception-name fragments and message phrases that mark a provider failure as an
#: authentication/authorization rejection — an invalid/expired/missing API key.
#: Rotating to another model won't fix this, so it is handled separately from the
#: retryable heuristic above (see ``_is_retryable_error``).
_AUTH_NAME_HINTS = ("authenticationerror", "permissiondenied", "unauthorized")
_AUTH_MSG_HINTS = ("invalid x-api-key", "invalid api key", "invalid_api_key",
                   "incorrect api key", "authentication_error", "authentication error",
                   "unauthorized", "no auth credentials", "missing api key",
                   "expired api key", "invalid bearer token", "permission denied")


def _iter_exc_chain(exc: BaseException, max_depth: int = 5):
    """Yield *exc* and up to *max_depth* more causes/contexts from the chain.

    Prefers ``__cause__`` (explicit ``raise X from Y``) over ``__context__``
    (implicit exception-during-exception).  A ``seen`` set guards against the
    theoretical cycle that a misbehaved exception could create.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    remaining = max_depth + 1
    while current is not None and remaining > 0 and id(current) not in seen:
        seen.add(id(current))
        yield current
        remaining -= 1
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        current = cause if cause is not None else context


def _classify_by_status(status: object) -> tuple[bool, bool] | None:
    """Map a numeric HTTP status to *(retryable, auth)* or ``None`` if not an int."""
    if not isinstance(status, int):
        return None
    if status in (401, 403):
        return (False, True)
    if status == 429 or status == 408 or (500 <= status < 600):
        return (True, False)
    if 400 <= status < 500:
        return (False, False)
    return None


def _classify_single_typed(exc: BaseException) -> tuple[bool, bool] | None:
    """Try to classify *exc* conclusively by type / status code.

    Returns *(retryable, auth)* when a definitive verdict is reached, or
    ``None`` when the exception is inconclusive (chain walk should continue).
    All attribute accesses are guarded with ``getattr`` to stay defensive.
    """
    # (a) status_code attribute — covers any SDK that exposes it directly
    try:
        status = getattr(exc, "status_code", None)
        verdict = _classify_by_status(status)
        if verdict is not None:
            return verdict
        # Also check exc.response.status_code (httpx, requests, …)
        response = getattr(exc, "response", None)
        if response is not None:
            r_status = getattr(response, "status_code", None)
            verdict = _classify_by_status(r_status)
            if verdict is not None:
                return verdict
    except Exception:  # noqa: BLE001
        pass  # defensive: a property raising must not propagate

    # (b) known exception types — import-guarded so missing SDKs are safe

    # httpx IS a jarn dependency; no guarding needed but kept symmetric.
    try:
        import httpx as _httpx  # noqa: PLC0415
        if isinstance(exc, _httpx.TimeoutException):
            return (True, False)
        if isinstance(exc, _httpx.HTTPStatusError):
            try:
                s = exc.response.status_code
                verdict = _classify_by_status(s)
                return verdict if verdict is not None else (False, False)
            except Exception:  # noqa: BLE001
                return (False, False)
    except ImportError:
        pass

    # Built-in / asyncio timeouts
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return (True, False)

    # Network-ish OS errors
    if isinstance(exc, ConnectionError):
        return (True, False)

    # anthropic SDK — optional; feature-detect by class hierarchy
    try:
        import anthropic as _anthropic  # noqa: PLC0415
        if isinstance(exc, _anthropic.APIStatusError):
            try:
                s = exc.status_code
                verdict = _classify_by_status(s)
                return verdict if verdict is not None else (False, False)
            except Exception:  # noqa: BLE001
                return (False, False)
        if isinstance(exc, _anthropic.RateLimitError):
            return (True, False)
        if isinstance(exc, _anthropic.AuthenticationError):
            return (False, True)
        if isinstance(exc, _anthropic.APIConnectionError):
            return (True, False)
    except ImportError:
        pass

    # openai SDK — optional
    try:
        import openai as _openai  # noqa: PLC0415
        if isinstance(exc, _openai.APIStatusError):
            try:
                s = exc.status_code
                verdict = _classify_by_status(s)
                return verdict if verdict is not None else (False, False)
            except Exception:  # noqa: BLE001
                return (False, False)
        if isinstance(exc, _openai.RateLimitError):
            return (True, False)
        if isinstance(exc, _openai.AuthenticationError):
            return (False, True)
        if isinstance(exc, _openai.APIConnectionError):
            return (True, False)
    except ImportError:
        pass

    return None  # inconclusive — caller should check the next cause in the chain


def classify_error(exc: BaseException) -> dict[str, Any]:
    """Classify *exc* as retryable/auth, walking the exception chain up to depth 5.

    Classification order per exception in the chain:

    1. ``status_code`` / ``response.status_code`` attributes (any SDK).
    2. Known exception types (``httpx``, ``asyncio``, ``anthropic``, ``openai``),
       import-guarded so missing providers don't break classification.
    3. Fall back to the existing substring heuristic table as a last resort (keeps
       provider wrappers that only stringify their errors working).

    The chain walk stops at the **first conclusive typed match**; inconclusive
    exceptions (no recognised type/attribute) move to the next ``__cause__`` /
    ``__context__``.

    Returns a ``dict`` with keys:

    * ``retryable`` (``bool``) — worth trying a model fallback.
    * ``auth`` (``bool``) — 401/403 key rejection; friendly hint shown instead of
      raw SDK JSON.
    * ``classified_by`` (``"type"`` | ``"heuristic"``) — observability tag;
      included in the ``ERROR`` event's ``data`` dict so transcripts can record it.
    """
    # Walk the chain looking for a conclusive typed verdict first.
    for cause in _iter_exc_chain(exc):
        verdict = _classify_single_typed(cause)
        if verdict is not None:
            retryable, auth = verdict
            return {"retryable": retryable, "auth": auth, "classified_by": "type"}

    # Heuristic fallback — the safety net for provider wrappers that only
    # stringify their errors.  We run only the name/message checks here; the
    # status_code branch that lived in the old ``_is_retryable_error`` /
    # ``_is_auth_error`` is now handled (conclusively) in the typed tier above.
    try:
        name = type(exc).__name__.lower()
        retryable_h = any(h in name for h in _RETRYABLE_NAME_HINTS)
        if not retryable_h:
            msg = str(exc).lower()
            retryable_h = any(h in msg for h in _RETRYABLE_MSG_HINTS)

        auth_h = any(h in name for h in _AUTH_NAME_HINTS)
        if not auth_h:
            msg_h = str(exc).lower()
            if "401" in msg_h or "403" in msg_h or any(h in msg_h for h in _AUTH_MSG_HINTS):
                auth_h = True
    except Exception:  # noqa: BLE001
        retryable_h = False
        auth_h = False

    return {"retryable": retryable_h, "auth": auth_h, "classified_by": "heuristic"}


def _is_retryable_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* represents a transient/capacity provider error.

    Delegates to :func:`classify_error` so both the typed tier (status codes,
    known SDK exception types, exception chain walk) and the heuristic fallback
    are applied consistently.  Kept for backward-compatibility; call sites that
    need the full classification dict should use :func:`classify_error` directly.

    Previously contained an inline ``status_code`` check (429 / 5xx) that is now
    handled by the typed tier in :func:`classify_error` — that branch is the
    deleted "dead" substring/attribute check noted in the T-1-8 report.
    """
    return classify_error(exc)["retryable"]


def _is_auth_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a 401/403 key rejection.

    Delegates to :func:`classify_error`.  Kept for backward-compatibility.

    The inline ``status_code in (401, 403)`` check from the original
    implementation is now handled by the typed tier in :func:`classify_error`
    (deleted dead branch; see T-1-8 report).
    """
    return classify_error(exc)["auth"]


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
