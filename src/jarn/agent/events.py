"""Event types and approval contract for the session driver."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jarn.permissions import (
    Action,
    PermissionResult,
    RememberScope,
)


class EventKind(str, Enum):
    TEXT = "text"
    REASONING = "reasoning"      # extended-thinking text (shown dim, secondary)
    TOOL_START = "tool_start"
    TOOL_PROGRESS = "tool_progress"  # incremental output while a long tool runs
    TOOL_END = "tool_end"
    APPROVAL = "approval"        # informational: how an approval was resolved
    NOTICE = "notice"
    ERROR = "error"
    DONE = "done"


@dataclass(slots=True)
class Event:
    kind: EventKind
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ToolProgress:
    """Incremental progress from a long-running *foreground* tool (live streaming).

    Emitted by a backend's ``execute`` from its worker thread while the command is
    still running, so the front-end can show a live tail + heartbeat under the
    running tool line instead of a bare spinner until the tool finishes. It carries
    only what the emitter knows — the ``command`` and, when the wiring layer sets
    them, the ``tool_call_id`` / ``tool_name`` that correlate this progress to a
    ``TOOL_START`` — plus the incremental ``chunk`` that triggered this update
    (empty on a heartbeat), a rolling ``tail`` (the last few output lines, already
    bounded by line count), ``elapsed`` seconds since the command started (computed
    from an injected clock so tests are deterministic), and ``heartbeat`` — ``True``
    when this update fired because output was quiet rather than because new bytes
    arrived.

    A :class:`ToolProgress` is converted to a ``TOOL_PROGRESS`` :class:`Event` by
    :func:`jarn.agent.stream_handlers.make_tool_progress_event` — the same seam that
    builds ``TOOL_START`` / ``TOOL_END`` events."""

    command: str = ""
    chunk: str = ""
    tail: str = ""
    elapsed: float = 0.0
    heartbeat: bool = False
    tool_call_id: str | None = None
    tool_name: str = "execute"


@dataclass(slots=True)
class SuggestedMemory:
    """A memory the agent proposes for the user to approve, edit, or decline.

    Carried on an :class:`ApprovalRequest` when the agent calls ``suggest_memory``.
    The approver surfaces a "Save this memory?" prompt and, on approval, writes it
    through the existing :class:`~jarn.memory.MemoryStore` (respecting the global
    vs project tier and the project's trust gating)."""

    name: str
    description: str
    body: str
    type: str = "project"
    #: ``"global"`` or ``"project"`` — which store tier to write to. Project writes
    #: are refused on an untrusted project (the approver reports why).
    scope: str = "project"


@dataclass(slots=True)
class ApprovalRequest:
    action: Action
    result: PermissionResult
    description: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    #: Set (to the proposed plan text) when this is a plan-mode handoff request
    #: from ``exit_plan_mode`` rather than an ordinary tool approval. The approver
    #: shows the plan and, on approval, escalates the permission mode.
    plan: str | None = None
    #: Set when this is an agent memory suggestion (``suggest_memory``) rather than
    #: an ordinary tool approval. The approver shows it and, on approval, writes it
    #: through the memory store.
    suggested_memory: SuggestedMemory | None = None


@dataclass(slots=True)
class ApprovalReply:
    approved: bool
    scope: RememberScope = RememberScope.ONCE
    message: str = ""           # reason shown to the model on rejection
    #: When the user chose "edit before apply", the tool args edited in $EDITOR.
    #: The turn resumes with a LangGraph ``edit`` decision carrying these args, so
    #: the *edited* content lands on disk instead of the agent's original. ``None``
    #: means a plain approve (run the tool with its original args).
    # TODO(per-hunk): edit-before-apply replaces the whole new content/replacement.
    # Per-hunk (partial) approval is deferred — it needs hunk parsing + partial
    # apply of a unified diff; not implemented in this pass (see fable-todo.md P4.B).
    edited_args: dict[str, Any] | None = None
    #: For a plan-mode handoff (``exit_plan_mode``): the permission mode the user
    #: chose to escalate to on approval (e.g. ``"auto-edit"``/``"ask"``). The
    #: approver applies it; ``None`` for ordinary approvals.
    plan_mode_target: str | None = None


# approver(request) -> reply
Approver = Callable[[ApprovalRequest], Awaitable[ApprovalReply]]


async def _auto_reject(request: ApprovalRequest) -> ApprovalReply:
    """Default approver used headless: deny anything that needs asking."""
    return ApprovalReply(approved=False, message="auto-denied (no interactive approver)")
