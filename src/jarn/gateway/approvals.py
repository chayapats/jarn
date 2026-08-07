"""Durable pending-approval routing map + park-shaped Approver (#37 / T-APPR-1).

Contract
--------
The LangGraph interrupt in ``<root>/.jarn/state.sqlite`` is the **source of
truth** for a parked turn. This module only owns a **routing/UI map** under
``~/.jarn/gateway/pending_approvals.json`` so the transport daemon can route a
callback (or restart re-card) to the right root/thread without scanning every
project checkpointer.

Park shape (β)
--------------
The gateway Approver **records then releases** — it does not block forever on a
live ``await``. Callers catch :exc:`ApprovalParked`; the interrupt stays in the
checkpointer until a later verdict re-drives the thread via
:meth:`~jarn.agent.session.SessionDriver.resume_pending_approval` /
``Command(resume=…)``.

No TTL: silence does not deny. Map rows remain until an explicit verdict,
cancel, or ``/new`` deletion.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jarn.agent.events import ApprovalReply, ApprovalRequest, Approver, Event
from jarn.config import paths
from jarn.permissions import RememberScope
from jarn.util.atomic import atomic_write_text, file_lock

_log = logging.getLogger("jarn")

#: Directory under ``JARN_HOME`` for gateway-owned state (routing maps, etc.).
GATEWAY_DIR_NAME = "gateway"

#: Durable token → routing row store. Routing/UI bookkeeping only — not SoT.
PENDING_APPROVALS_FILENAME = "pending_approvals.json"

#: Wire schema version for the JSON document (bump on incompatible shape).
_STORE_VERSION = 1


class ApprovalParked(Exception):
    """Raised by a park-shaped Approver after recording a pending ask.

    The turn must release without delivering ``Command(resume=…)`` so the
    LangGraph interrupt remains parked in ``state.sqlite``. Carries the minted
    routing ``token`` (and the original request) for the daemon to card.
    """

    def __init__(self, token: str, request: ApprovalRequest) -> None:
        self.token = token
        self.request = request
        super().__init__(f"approval parked token={token}")


@dataclass(slots=True, frozen=True)
class PendingApproval:
    """One gateway routing row for a parked approval (#37)."""

    token: str
    root: str
    thread_id: str
    interrupt_id: str
    chat_id: int
    message_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PendingApproval:
        mid = data.get("message_id")
        if mid is not None and not isinstance(mid, int):
            raise ValueError("message_id must be an int or null")
        chat_id = data["chat_id"]
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            raise ValueError("chat_id must be an int")
        return cls(
            token=str(data["token"]),
            root=str(data["root"]),
            thread_id=str(data["thread_id"]),
            interrupt_id=str(data["interrupt_id"]),
            chat_id=chat_id,
            message_id=mid,
        )


def pending_approvals_path(*, home: Path | None = None) -> Path:
    """Return ``~/.jarn/gateway/pending_approvals.json`` (or under *home*)."""
    base = Path(home) if home is not None else paths.global_home()
    return base / GATEWAY_DIR_NAME / PENDING_APPROVALS_FILENAME


def mint_approval_token() -> str:
    """Opaque callback token; no TTL (silence ≠ deny)."""
    return secrets.token_urlsafe(24)


class PendingApprovalMap:
    """CRUD store for ``token → {root, thread_id, interrupt_id, chat_id, message_id}``.

    Cross-process safe via :func:`~jarn.util.atomic.file_lock` + atomic publish.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else pending_approvals_path()

    def put(self, record: PendingApproval) -> PendingApproval:
        """Insert or replace the row for ``record.token``."""
        with file_lock(self.path):
            data = self._load_unlocked()
            data[record.token] = record.to_dict()
            self._save_unlocked(data)
        return record

    def get(self, token: str) -> PendingApproval | None:
        """Return the row for *token*, or ``None`` if absent."""
        with file_lock(self.path):
            raw = self._load_unlocked().get(token)
        if raw is None:
            return None
        return PendingApproval.from_dict(raw)

    def delete(self, token: str) -> PendingApproval | None:
        """Remove *token* if present; return the deleted row (else ``None``)."""
        with file_lock(self.path):
            data = self._load_unlocked()
            raw = data.pop(token, None)
            if raw is None:
                return None
            self._save_unlocked(data)
            return PendingApproval.from_dict(raw)

    def list(self) -> list[PendingApproval]:
        """All pending rows — used on gateway restart to re-card (#37)."""
        with file_lock(self.path):
            data = self._load_unlocked()
        out: list[PendingApproval] = []
        for raw in data.values():
            try:
                out.append(PendingApproval.from_dict(raw))
            except (KeyError, TypeError, ValueError) as exc:
                _log.warning("skipping corrupt pending-approval row: %s", exc)
        return out

    def clear(self) -> int:
        """Delete every row. Returns how many were removed."""
        with file_lock(self.path):
            data = self._load_unlocked()
            n = len(data)
            self._save_unlocked({})
        return n

    def _load_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("pending-approval map unreadable (%s); starting empty", exc)
            return {}
        if not isinstance(doc, dict):
            return {}
        pending = doc.get("pending", doc)
        if not isinstance(pending, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in pending.items():
            if isinstance(value, dict):
                out[str(key)] = value
        return out

    def _save_unlocked(self, pending: dict[str, dict[str, Any]]) -> None:
        doc = {"version": _STORE_VERSION, "pending": pending}
        text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        atomic_write_text(self.path, text, mode=0o600)


def record_pending_approval(
    *,
    root: Path | str,
    thread_id: str,
    interrupt_id: str,
    chat_id: int,
    message_id: int | None = None,
    token: str | None = None,
    store: PendingApprovalMap | None = None,
) -> PendingApproval:
    """Persist one routing row and return it (minting *token* when omitted)."""
    map_ = store or PendingApprovalMap()
    record = PendingApproval(
        token=token or mint_approval_token(),
        root=str(Path(root).expanduser().resolve()),
        thread_id=thread_id,
        interrupt_id=interrupt_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    return map_.put(record)


def make_park_approver(
    *,
    root: Path | str,
    thread_id: str,
    interrupt_id: str,
    chat_id: int,
    message_id: int | None = None,
    store: PendingApprovalMap | None = None,
    token_factory: Callable[[], str] | None = None,
    on_park: Callable[[PendingApproval, ApprovalRequest], Awaitable[None] | None]
    | None = None,
) -> Approver:
    """Build an :class:`~jarn.agent.events.Approver` that parks then releases.

    Records ``{token → root, thread_id, interrupt_id, chat_id, message_id}``,
    optionally awaits *on_park* (e.g. emit :class:`ApprovalAskFrame` / send a
    Telegram card), then raises :exc:`ApprovalParked` so the worker does **not**
    deliver a resume ``Command``. Suitable for the gateway; not for the TUI.
    """
    map_ = store or PendingApprovalMap()
    mint = token_factory or mint_approval_token
    root_s = str(Path(root).expanduser().resolve())

    async def _park(request: ApprovalRequest) -> ApprovalReply:
        record = PendingApproval(
            token=mint(),
            root=root_s,
            thread_id=thread_id,
            interrupt_id=interrupt_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        map_.put(record)
        if on_park is not None:
            maybe = on_park(record, request)
            if maybe is not None:
                await maybe
        raise ApprovalParked(record.token, request)

    return _park


def make_verdict_approver(
    *,
    approved: bool,
    scope: RememberScope | str = RememberScope.ONCE,
    message: str = "",
    plan_mode_target: str | None = None,
    edited_args: dict[str, Any] | None = None,
) -> Approver:
    """Approver that returns a fixed verdict (warm-worker / callback path).

    Remote ALWAYS is out of v1 (#39); ``scope`` should be ``once`` or ``session``.
    Extra :class:`ApprovalReply` fields are accepted for schema foresight.
    """
    remember = (
        scope if isinstance(scope, RememberScope) else RememberScope(str(scope).lower())
    )
    if remember is RememberScope.ALWAYS:
        # Floor: remote ALWAYS is forbidden in v1 — downgrade to SESSION.
        remember = RememberScope.SESSION
    reply = ApprovalReply(
        approved=approved,
        scope=remember,
        message=message,
        edited_args=edited_args,
        plan_mode_target=plan_mode_target,
    )

    async def _verdict(_request: ApprovalRequest) -> ApprovalReply:
        return reply

    return _verdict


async def resume_parked_approval(
    driver: Any,
    *,
    token: str,
    approved: bool,
    scope: RememberScope | str = RememberScope.ONCE,
    message: str = "",
    plan_mode_target: str | None = None,
    edited_args: dict[str, Any] | None = None,
    store: PendingApprovalMap | None = None,
    delete_on_complete: bool = True,
) -> AsyncIterator[Event]:
    """Warm a worker path: install a verdict Approver and resume the interrupt.

    Looks up *token* in the gateway map, swaps ``driver.approver`` for a
    fixed-verdict Approver, then streams
    :meth:`~jarn.agent.session.SessionDriver.resume_pending_approval`.

    The LangGraph interrupt in ``<root>/.jarn/state.sqlite`` remains authoritative;
    this helper only routes + injects the verdict. Deletes the map row after the
    resume generator completes when *delete_on_complete* is true.

    TODO(T-WKR-1): daemon should spawn/warm the per-root worker, bind
    ``driver.thread_id`` to the map row, then call this. Caller must ensure the
    driver's checkpointer is the project's ``state.sqlite`` for ``record.root``.

    TODO(#39): plumb richer remote decision sets once the permission posture
    ticket settles button labels / ALWAYS / memory edit paths.
    """
    map_ = store or PendingApprovalMap()
    record = map_.get(token)
    if record is None:
        raise KeyError(f"unknown approval token: {token}")

    # Prefer the mapped thread when the driver was constructed for another one.
    if getattr(driver, "thread_id", None) not in (None, record.thread_id):
        _log.warning(
            "resume token=%s: driver thread_id=%s != map thread_id=%s; using map",
            token,
            driver.thread_id,
            record.thread_id,
        )
        driver.thread_id = record.thread_id

    previous = getattr(driver, "approver", None)
    driver.approver = make_verdict_approver(
        approved=approved,
        scope=scope,
        message=message,
        plan_mode_target=plan_mode_target,
        edited_args=edited_args,
    )
    try:
        async for ev in driver.resume_pending_approval():
            yield ev
    finally:
        if previous is not None:
            driver.approver = previous
        if delete_on_complete:
            map_.delete(token)


__all__ = [
    "GATEWAY_DIR_NAME",
    "PENDING_APPROVALS_FILENAME",
    "ApprovalParked",
    "PendingApproval",
    "PendingApprovalMap",
    "make_park_approver",
    "make_verdict_approver",
    "mint_approval_token",
    "pending_approvals_path",
    "record_pending_approval",
    "resume_parked_approval",
]
