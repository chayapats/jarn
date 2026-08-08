"""Durable pending-approval map + park Approver (#37 / T-APPR-1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarn.agent.events import ApprovalReply, ApprovalRequest, Event, EventKind
from jarn.gateway.approvals import (
    PENDING_APPROVALS_FILENAME,
    ApprovalParked,
    PendingApproval,
    PendingApprovalMap,
    make_park_approver,
    make_verdict_approver,
    mint_approval_token,
    pending_approvals_path,
    record_pending_approval,
    resume_parked_approval,
)
from jarn.permissions import (
    Action,
    ActionKind,
    Decision,
    PermissionResult,
    RememberScope,
)


def _ask(tool: str = "execute", target: str = "rm -rf /") -> ApprovalRequest:
    return ApprovalRequest(
        action=Action(ActionKind.SHELL, target=target, tool=tool),
        result=PermissionResult(Decision.ASK, "needs confirmation"),
        description="run command",
        args={"command": target},
    )


def test_pending_path_under_jarn_home(isolated_home: Path) -> None:
    assert pending_approvals_path() == isolated_home / "gateway" / PENDING_APPROVALS_FILENAME


def test_map_put_get_delete_list(isolated_home: Path) -> None:
    store = PendingApprovalMap()
    assert store.list() == []

    a = PendingApproval(
        token="tok-a",
        root="/repos/a",
        thread_id="th-1",
        interrupt_id="intr-1",
        chat_id=42,
        message_id=7,
    )
    b = PendingApproval(
        token="tok-b",
        root="/repos/b",
        thread_id="th-2",
        interrupt_id="intr-2",
        chat_id=99,
        message_id=None,
    )
    store.put(a)
    store.put(b)

    assert store.get("tok-a") == a
    assert store.get("tok-b") == b
    assert store.get("missing") is None

    listed = {r.token: r for r in store.list()}
    assert set(listed) == {"tok-a", "tok-b"}
    assert listed["tok-a"].message_id == 7
    assert listed["tok-b"].message_id is None

    # Survives a fresh map instance (durable file under JARN_HOME).
    reload = PendingApprovalMap()
    assert reload.get("tok-a") == a

    deleted = store.delete("tok-a")
    assert deleted == a
    assert store.get("tok-a") is None
    assert store.delete("tok-a") is None
    assert [r.token for r in store.list()] == ["tok-b"]


def test_map_put_replaces_same_token(isolated_home: Path) -> None:
    store = PendingApprovalMap()
    store.put(
        PendingApproval(
            token="t",
            root="/r",
            thread_id="th",
            interrupt_id="i1",
            chat_id=1,
            message_id=None,
        )
    )
    store.put(
        PendingApproval(
            token="t",
            root="/r",
            thread_id="th",
            interrupt_id="i1",
            chat_id=1,
            message_id=99,
        )
    )
    assert store.get("t") is not None
    assert store.get("t").message_id == 99
    assert len(store.list()) == 1


def test_map_card_metadata_is_backward_compatible_and_atomically_updated(
    isolated_home: Path,
) -> None:
    store = PendingApprovalMap()
    # Version-1 rows created before card persistence remain readable.
    original = PendingApproval(
        token="legacy",
        root="/old/root",
        thread_id="thread",
        interrupt_id="interrupt",
        chat_id=0,
    )
    store.put(original)
    assert store.get("legacy") == original
    assert store.get("legacy").card is None

    new_root = isolated_home / "new-root"
    updated = store.attach_card(
        "legacy",
        root=new_root,
        chat_id=42,
        card={"action": "execute", "description": "run command"},
    )
    assert updated is not None
    assert updated.root == str(new_root.resolve())
    assert updated.chat_id == 42
    assert updated.card == {"action": "execute", "description": "run command"}

    with_message = store.set_message_id("legacy", 99)
    assert with_message is not None
    assert with_message.message_id == 99
    assert store.attach_card("missing", root="/x", chat_id=1, card={}) is None


def test_record_pending_approval_mints_token(isolated_home: Path, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    rec = record_pending_approval(
        root=root,
        thread_id="thread-x",
        interrupt_id="intr-x",
        chat_id=12345,
        message_id=3,
    )
    assert rec.token
    assert rec.root == str(root.resolve())
    assert PendingApprovalMap().get(rec.token) == rec


@pytest.mark.asyncio
async def test_park_approver_records_and_releases(isolated_home: Path, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    seen: list[tuple[str, str]] = []

    async def on_park(record: PendingApproval, request: ApprovalRequest) -> None:
        seen.append((record.token, request.action.tool or ""))

    approver = make_park_approver(
        root=root,
        thread_id="th-park",
        interrupt_id="intr-park",
        chat_id=55,
        message_id=8,
        token_factory=lambda: "fixed-token",
        on_park=on_park,
    )

    with pytest.raises(ApprovalParked) as excinfo:
        await approver(_ask())

    assert excinfo.value.token == "fixed-token"
    assert seen == [("fixed-token", "execute")]

    stored = PendingApprovalMap().get("fixed-token")
    assert stored is not None
    assert stored.thread_id == "th-park"
    assert stored.interrupt_id == "intr-park"
    assert stored.chat_id == 55
    assert stored.message_id == 8
    assert stored.root == str(root.resolve())


@pytest.mark.asyncio
async def test_verdict_approver_returns_fixed_reply() -> None:
    approver = make_verdict_approver(
        approved=True,
        scope="session",
        message="",
        plan_mode_target="auto-edit",
    )
    reply = await approver(_ask())
    assert reply == ApprovalReply(
        approved=True,
        scope=RememberScope.SESSION,
        message="",
        plan_mode_target="auto-edit",
    )


@pytest.mark.asyncio
async def test_verdict_approver_downgrades_remote_always() -> None:
    reply = await make_verdict_approver(approved=True, scope="always")(_ask())
    assert reply.scope is RememberScope.SESSION


@pytest.mark.asyncio
async def test_resume_parked_approval_wires_verdict_and_deletes_token(
    isolated_home: Path, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    record = record_pending_approval(
        root=root,
        thread_id="th-resume",
        interrupt_id="intr-resume",
        chat_id=1,
        message_id=2,
        token="resume-tok",
    )

    calls: list[ApprovalReply] = []

    class _FakeDriver:
        thread_id = "other-thread"
        approver = make_verdict_approver(approved=False)

        async def resume_pending_approval(self, *args: Any, **kwargs: Any):
            reply = await self.approver(_ask())
            calls.append(reply)
            yield Event(EventKind.APPROVAL, text="approved")
            yield Event(EventKind.DONE)

    driver = _FakeDriver()
    events = [
        ev
        async for ev in resume_parked_approval(
            driver,
            token=record.token,
            approved=True,
            scope="once",
        )
    ]

    assert driver.thread_id == "th-resume"
    assert [e.kind for e in events] == [EventKind.APPROVAL, EventKind.DONE]
    assert calls == [ApprovalReply(approved=True, scope=RememberScope.ONCE, message="")]
    assert PendingApprovalMap().get("resume-tok") is None


@pytest.mark.asyncio
async def test_resume_unknown_token_raises(isolated_home: Path) -> None:
    class _Driver:
        thread_id = "t"
        approver = make_verdict_approver(approved=False)

        async def resume_pending_approval(self, *args: Any, **kwargs: Any):
            if False:  # pragma: no cover
                yield Event(EventKind.DONE)

    with pytest.raises(KeyError, match="unknown approval token"):
        async for _ in resume_parked_approval(_Driver(), token="nope", approved=True):
            pass


def test_mint_token_is_opaque_and_unique() -> None:
    a, b = mint_approval_token(), mint_approval_token()
    assert a != b
    assert len(a) >= 16
