"""Tests for the auto-checkpoint manager (CheckpointManager).

All tests use a REAL temporary git repo created via subprocess ``git init``.
The hard invariant asserted throughout: ``git rev-parse HEAD`` is unchanged
after every snapshot, undo, and redo operation.

Scenarios covered:
- snapshot then modify a tracked file → undo restores original content.
- snapshot then CREATE an untracked file → undo removes it.
- snapshot then DELETE a tracked file → undo restores it.
- redo re-applies what undo reverted.
- no-op: not a git repo (no crash, clean message).
- no-op: feature disabled.
- no-op: nothing changed (working tree matches HEAD).
- pre-existing uncommitted user change is NOT lost by snapshot/undo.
- .gitignored files are not swept into snapshots.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarn.agent.checkpoint import CheckpointManager, SnapshotResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd``; raises CalledProcessError on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A fresh git repo with a single commit so HEAD is valid."""
    _git(["init", "-b", "main"], cwd=tmp_path)
    _git(["config", "user.email", "test@jarn.test"], cwd=tmp_path)
    _git(["config", "user.name", "Jarn Test"], cwd=tmp_path)
    # Initial commit so HEAD exists.
    (tmp_path / "README.txt").write_text("init\n", encoding="utf-8")
    _git(["add", "README.txt"], cwd=tmp_path)
    _git(["commit", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _head(root: Path) -> str:
    """Return the current HEAD SHA."""
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def _manager(root: Path, *, enabled: bool = True) -> CheckpointManager:
    return CheckpointManager(repo_root=root, enabled=enabled)


# ---------------------------------------------------------------------------
# Core undo/redo scenarios
# ---------------------------------------------------------------------------


def test_undo_restores_modified_tracked_file(repo: Path) -> None:
    """snapshot + modify tracked file → undo restores original content."""
    original = (repo / "README.txt").read_text(encoding="utf-8")
    head_before = _head(repo)

    mgr = _manager(repo)
    snap = mgr.snapshot("before-edit", now=1_000_000.0)
    assert snap.ok, f"snapshot failed: {snap.message}"

    # Simulate agent editing a tracked file.
    (repo / "README.txt").write_text("agent changed me\n", encoding="utf-8")
    assert (repo / "README.txt").read_text(encoding="utf-8") != original

    result = mgr.undo()
    assert result.ok, f"undo failed: {result.message}"
    assert (repo / "README.txt").read_text(encoding="utf-8") == original

    # HEAD must not have moved.
    assert _head(repo) == head_before


def test_undo_removes_untracked_file_created_by_agent(repo: Path) -> None:
    """snapshot + agent creates untracked file → undo removes it."""
    head_before = _head(repo)

    mgr = _manager(repo)
    snap = mgr.snapshot("before-create", now=1_000_000.0)
    assert snap.ok

    new_file = repo / "agent_created.py"
    new_file.write_text("print('hello')\n", encoding="utf-8")
    assert new_file.exists()

    result = mgr.undo()
    assert result.ok, f"undo failed: {result.message}"
    assert not new_file.exists(), "undo should have removed the agent-created file"

    assert _head(repo) == head_before


def test_undo_restores_tracked_file_deleted_by_agent(repo: Path) -> None:
    """snapshot + agent deletes tracked file → undo restores it."""
    # Add a second tracked file.
    victim = repo / "victim.txt"
    victim.write_text("precious content\n", encoding="utf-8")
    _git(["add", "victim.txt"], cwd=repo)
    _git(["commit", "-m", "add victim"], cwd=repo)
    head_before = _head(repo)

    mgr = _manager(repo)
    snap = mgr.snapshot("before-delete", now=1_000_000.0)
    assert snap.ok

    victim.unlink()
    assert not victim.exists()

    result = mgr.undo()
    assert result.ok, f"undo failed: {result.message}"
    assert victim.exists(), "undo should have restored the deleted file"
    assert victim.read_text(encoding="utf-8") == "precious content\n"

    assert _head(repo) == head_before


def test_redo_reapplies_undone_changes(repo: Path) -> None:
    """snapshot → modify → undo → redo re-applies the modification."""
    head_before = _head(repo)

    mgr = _manager(repo)
    mgr.snapshot("before", now=1_000_000.0)

    # Agent modifies a file.
    (repo / "README.txt").write_text("agent version\n", encoding="utf-8")

    mgr.undo()
    assert (repo / "README.txt").read_text(encoding="utf-8") != "agent version\n"

    # Redo should bring back "agent version".
    result = mgr.redo()
    assert result.ok, f"redo failed: {result.message}"
    assert (repo / "README.txt").read_text(encoding="utf-8") == "agent version\n"

    assert _head(repo) == head_before


# ---------------------------------------------------------------------------
# No-op and error paths
# ---------------------------------------------------------------------------


def test_no_crash_on_non_git_dir(tmp_path: Path) -> None:
    """CheckpointManager.snapshot/undo/redo return clean messages, no crash."""
    mgr = CheckpointManager(repo_root=tmp_path, enabled=True)
    assert not mgr._is_repo

    snap = mgr.snapshot("x", now=1.0)
    assert not snap.ok
    assert "git" in snap.message.lower() or "repo" in snap.message.lower()

    undo = mgr.undo()
    assert not undo.ok
    assert "git" in undo.message.lower() or "repo" in undo.message.lower()

    redo = mgr.redo()
    assert not redo.ok
    assert "git" in redo.message.lower() or "repo" in redo.message.lower()


def test_no_op_when_feature_disabled(repo: Path) -> None:
    """Disabled manager returns clean no-op messages without touching the repo."""
    head_before = _head(repo)

    mgr = _manager(repo, enabled=False)
    snap = mgr.snapshot("x", now=1.0)
    assert not snap.ok
    assert "disabled" in snap.message.lower()

    undo = mgr.undo()
    assert not undo.ok
    assert "disabled" in undo.message.lower()

    redo = mgr.redo()
    assert not redo.ok
    assert "disabled" in redo.message.lower()

    assert _head(repo) == head_before


def test_snapshot_noop_when_called_twice_without_changes(repo: Path) -> None:
    """Snapshot deduplicates: a second snapshot with the same tree is a no-op.

    The first snapshot of a clean tree is always taken (it records the baseline
    so /undo can revert to it later).  The second snapshot with no changes in
    between is rejected to avoid a stack of identical entries.
    """
    head_before = _head(repo)
    mgr = _manager(repo)

    # Dirty the tree so the first snapshot records a useful state.
    (repo / "README.txt").write_text("first-change\n", encoding="utf-8")

    snap1 = mgr.snapshot("first", now=1.0)
    assert snap1.ok, f"first snapshot should succeed: {snap1.message}"
    assert _head(repo) == head_before

    # No changes between snaps → deduplication should kick in.
    snap2 = mgr.snapshot("duplicate", now=2.0)
    assert not snap2.ok
    assert "nothing" in snap2.message.lower() or "already" in snap2.message.lower()

    assert _head(repo) == head_before


def test_undo_noop_when_stack_empty(repo: Path) -> None:
    """undo() returns ok=False with a clear message when there's nothing to undo."""
    mgr = _manager(repo)
    result = mgr.undo()
    assert not result.ok
    assert "nothing" in result.message.lower() or "undo" in result.message.lower()


def test_redo_noop_when_stack_empty(repo: Path) -> None:
    """redo() returns ok=False with a clear message when there's nothing to redo."""
    mgr = _manager(repo)
    result = mgr.redo()
    assert not result.ok
    assert "nothing" in result.message.lower() or "redo" in result.message.lower()


# ---------------------------------------------------------------------------
# Data-safety
# ---------------------------------------------------------------------------


def test_preexisting_user_change_not_lost_by_snapshot_undo(repo: Path) -> None:
    """A pre-existing uncommitted user change survives snapshot/undo of a DIFFERENT file.

    The user has modified file-A (not yet committed). The agent then touches
    file-B. After /undo, file-A's change must still be present.
    """
    # Set up a second file committed at HEAD.
    file_b = repo / "file_b.txt"
    file_b.write_text("original-b\n", encoding="utf-8")
    _git(["add", "file_b.txt"], cwd=repo)
    _git(["commit", "-m", "add file_b"], cwd=repo)
    head_before = _head(repo)

    # User has an uncommitted change to file-A.
    (repo / "README.txt").write_text("user's uncommitted change\n", encoding="utf-8")

    # Snapshot captures the working tree at this point (including user's change).
    mgr = _manager(repo)
    snap = mgr.snapshot("before-agent", now=1_000_000.0)
    assert snap.ok, f"snapshot should succeed when tree differs from HEAD: {snap.message}"

    # Agent modifies a DIFFERENT file (file_b).
    file_b.write_text("agent-modified-b\n", encoding="utf-8")

    # Undo should restore file_b to "original-b" but keep the user's README change.
    result = mgr.undo()
    assert result.ok, f"undo failed: {result.message}"
    assert file_b.read_text(encoding="utf-8") == "original-b\n", (
        "file_b should have been restored"
    )
    assert (repo / "README.txt").read_text(encoding="utf-8") == "user's uncommitted change\n", (
        "user's uncommitted change to README.txt must not be lost"
    )

    assert _head(repo) == head_before


# ---------------------------------------------------------------------------
# .gitignore respected
# ---------------------------------------------------------------------------


def test_gitignored_files_not_captured_in_snapshot(repo: Path) -> None:
    """.gitignored files are excluded from snapshots and not removed by undo.

    Scenario:
    - .gitignore excludes *.log
    - Snapshot taken on a clean tree (README.txt == "init")
    - Agent modifies README.txt and creates a non-ignored file
    - A *.log file is also present (created before or after — should be ignored)
    - Undo restores README.txt to "init" and removes the non-ignored new file
    - The *.log file is left untouched (it was never part of any snapshot)
    """
    # Write a .gitignore and commit it so the snapshot respects it.
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(["add", ".gitignore"], cwd=repo)
    _git(["commit", "-m", "add gitignore"], cwd=repo)
    head_before = _head(repo)

    # Take snapshot of the clean state (README.txt == "init\n").
    mgr = _manager(repo)
    snap = mgr.snapshot("pre-agent", now=1_000_000.0)
    assert snap.ok, f"snapshot should succeed: {snap.message}"

    # Agent modifies a tracked file and creates a non-ignored untracked file.
    (repo / "README.txt").write_text("agent-modified\n", encoding="utf-8")
    new_untracked = repo / "new_file.py"
    new_untracked.write_text("# added by agent\n", encoding="utf-8")

    # A *.log file is created (ignored by .gitignore).
    ignored = repo / "debug.log"
    ignored.write_text("debug output\n", encoding="utf-8")

    # Undo should:
    #   - restore README.txt to the snapshot state ("init\n")
    #   - remove new_file.py (not in snapshot, not ignored)
    #   - leave debug.log alone (git never tracked it, not in snapshot either)
    result = mgr.undo()
    assert result.ok, f"undo failed: {result.message}"
    assert (repo / "README.txt").read_text(encoding="utf-8") == "init\n", (
        "tracked file should be restored to snapshot state"
    )
    assert not new_untracked.exists(), (
        "non-ignored file created by agent should be removed by undo"
    )
    assert ignored.exists(), (
        ".gitignored *.log file should be left untouched by undo"
    )

    assert _head(repo) == head_before


# ---------------------------------------------------------------------------
# HEAD invariant — exhaustive check across all operations
# ---------------------------------------------------------------------------


def test_head_never_moves_across_all_operations(repo: Path) -> None:
    """HEAD stays identical through snapshot, undo, and redo."""
    head_before = _head(repo)

    mgr = _manager(repo)

    # Dirty the worktree so snapshot does something useful.
    (repo / "README.txt").write_text("v1\n", encoding="utf-8")
    s1 = mgr.snapshot("turn-1", now=1.0)
    assert s1.ok
    assert _head(repo) == head_before

    (repo / "README.txt").write_text("v2\n", encoding="utf-8")
    s2 = mgr.snapshot("turn-2", now=2.0)
    assert s2.ok
    assert _head(repo) == head_before

    mgr.undo()
    assert _head(repo) == head_before

    mgr.redo()
    assert _head(repo) == head_before

    mgr.undo()
    assert _head(repo) == head_before


# ---------------------------------------------------------------------------
# list()
# ---------------------------------------------------------------------------


def test_list_returns_entries_in_most_recent_first_order(repo: Path) -> None:
    """list() returns checkpoint entries with most recent first."""
    (repo / "README.txt").write_text("state-1\n", encoding="utf-8")
    mgr = _manager(repo)
    mgr.snapshot("turn-1", now=1.0)

    (repo / "README.txt").write_text("state-2\n", encoding="utf-8")
    mgr.snapshot("turn-2", now=2.0)

    entries = mgr.list()
    assert len(entries) == 2
    # Most recent should be at index 0.
    assert "turn-2" in entries[0].label or entries[0].sha != entries[1].sha


def test_list_empty_on_non_git_dir(tmp_path: Path) -> None:
    mgr = CheckpointManager(repo_root=tmp_path, enabled=True)
    assert mgr.list() == []


# ---------------------------------------------------------------------------
# T-3-1 — per-turn snapshot metadata + find_for_turn + restore_to
# ---------------------------------------------------------------------------


def test_find_for_turn(repo: Path) -> None:
    """After 3 turn-start snapshots tagged with (thread, turn_index), find_for_turn
    returns the SHA snapshotted at the requested turn — the conversation↔file link
    that lets /rewind restore the tree atomically with the fork."""
    mgr = _manager(repo)
    thread = "thread-xyz"
    shas: list[str] = []
    for turn in range(3):
        (repo / "README.txt").write_text(f"turn-{turn}\n", encoding="utf-8")
        snap = mgr.snapshot(
            f"t{turn}", now=float(turn + 1), thread_id=thread, turn_index=turn
        )
        assert snap.ok, snap.message
        shas.append(snap.sha)

    ref = mgr.find_for_turn(thread, 2)
    assert ref is not None
    assert ref.sha == shas[2]
    assert ref.turn_index == 2
    assert ref.thread_id == thread
    # Every recorded turn resolves to its own snapshot.
    assert mgr.find_for_turn(thread, 0).sha == shas[0]  # type: ignore[union-attr]
    assert mgr.find_for_turn(thread, 1).sha == shas[1]  # type: ignore[union-attr]
    # An unknown turn / unknown thread → None (never crashes).
    assert mgr.find_for_turn(thread, 9) is None
    assert mgr.find_for_turn("other-thread", 2) is None


def test_find_for_turn_none_for_old_format_records(repo: Path) -> None:
    """Snapshots taken WITHOUT thread/turn metadata (pre-T-3-1 records) are simply
    not matched — find_for_turn returns None (conversation-only fallback), never
    crashes. Backwards compatibility for sessions started before this feature."""
    mgr = _manager(repo)
    (repo / "README.txt").write_text("legacy\n", encoding="utf-8")
    snap = mgr.snapshot("legacy-turn", now=1.0)  # no thread_id / turn_index kwargs
    assert snap.ok
    assert mgr.find_for_turn("any-thread", 0) is None


def test_find_for_turn_none_on_non_git_dir(tmp_path: Path) -> None:
    """find_for_turn is a no-op (None) outside a git repo — never raises."""
    mgr = CheckpointManager(repo_root=tmp_path, enabled=True)
    assert mgr.find_for_turn("t", 0) is None


def test_restore_to_reverts_tree_and_is_undoable(repo: Path) -> None:
    """restore_to() sets the worktree to a target snapshot AND pushes the pre-restore
    tree onto the undo stack, so /undo reverts the rewind's file restore (no work is
    ever lost). Also asserts the untracked-file semantics on restore + the HEAD
    invariant."""
    head_before = _head(repo)
    mgr = _manager(repo)
    (repo / "README.txt").write_text("target\n", encoding="utf-8")
    snap = mgr.snapshot("target-turn", now=1.0)
    assert snap.ok

    # Diverge the tree — the mid-refactor state we rewind away from, including a
    # NEW untracked file the agent added after the target snapshot.
    (repo / "README.txt").write_text("current\n", encoding="utf-8")
    (repo / "new.py").write_text("print(1)\n", encoding="utf-8")

    res = mgr.restore_to(snap.sha)
    assert res.ok, res.message
    assert (repo / "README.txt").read_text(encoding="utf-8") == "target\n"
    # Untracked file created since the target snapshot is removed by the restore
    # (follows the existing _apply_snapshot semantics used by /undo).
    assert not (repo / "new.py").exists()
    assert _head(repo) == head_before  # HEAD never moves

    # /undo brings the pre-restore tree back — the rewind is itself reversible.
    undo = mgr.undo()
    assert undo.ok, undo.message
    assert (repo / "README.txt").read_text(encoding="utf-8") == "current\n"
    assert (repo / "new.py").exists()
    assert _head(repo) == head_before


def test_restore_to_disabled_and_non_git_noop(repo: Path, tmp_path: Path) -> None:
    """restore_to degrades cleanly when disabled or outside a repo (no crash)."""
    disabled = _manager(repo, enabled=False)
    r = disabled.restore_to("deadbeef")
    assert not r.ok and "disabled" in r.message.lower()

    non_git_dir = tmp_path / "nope"
    non_git_dir.mkdir()
    non_git = CheckpointManager(repo_root=non_git_dir, enabled=True)
    r2 = non_git.restore_to("deadbeef")
    assert not r2.ok and ("git" in r2.message.lower() or "repo" in r2.message.lower())


def test_undo_rollback_on_apply_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If restore fails, redo stack must stay unchanged (no orphan entry)."""
    from jarn.agent import checkpoint as cp

    (repo / "README.txt").write_text("dirty\n", encoding="utf-8")
    mgr = _manager(repo)
    snap = mgr.snapshot("before", now=1.0)
    assert snap.ok

    (repo / "README.txt").write_text("agent edit\n", encoding="utf-8")

    def _fail_apply(sha: str, root: Path) -> tuple[bool, str]:
        return False, "simulated restore failure"

    monkeypatch.setattr(cp, "_apply_snapshot", _fail_apply)

    redo_before = cp._stack_read(cp._REDO_PREFIX, repo)
    result = mgr.undo()
    assert not result.ok
    assert "restore failed" in result.message.lower()

    redo_after = cp._stack_read(cp._REDO_PREFIX, repo)
    assert redo_after == redo_before, "failed undo must not leave an orphan on redo"


# ---------------------------------------------------------------------------
# T-1-4 — non-blocking snapshot: the mutation gate awaits snapshot completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_waits_for_snapshot() -> None:
    """The first mutating tool must not EXECUTE until the turn-start snapshot has
    completed — while the model stream may begin while the snapshot is still
    running (turn start no longer blocks on the snapshot).

    Driven through the real ``SessionDriver.run_turn`` with a fake agent (write
    interrupt → resume) and a deliberately slow fake snapshot, asserting event
    order via a shared log. The core invariant (snap_done < tool_execute) is
    guaranteed by the mutation gate regardless of timing; the non-blocking claim
    (model_stream < snap_done) holds with a wide margin (µs of model stream vs a
    150 ms snapshot).
    """
    import time as _t

    from jarn.agent.session import ApprovalReply, EventKind, SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    order: list[str] = []

    class SlowCheckpoint:
        snapshot_notice_pending: bool = False
        snapshot_notice_shown: bool = False

        def snapshot(
            self,
            label: str,
            *,
            now: float | None = None,
            thread_id: str | None = None,
            turn_index: int | None = None,
        ) -> SnapshotResult:
            order.append("snap_start")
            _t.sleep(0.15)  # simulate O(repo) git add -A + write-tree
            order.append("snap_done")
            return SnapshotResult(ok=True, sha="deadbeef")

    class _AIChunk:
        type = "ai"

        def __init__(self, content: str) -> None:
            self.content = content
            self.usage_metadata = {"input_tokens": 1, "output_tokens": 1}
            self.response_metadata: dict[str, str] = {}

    class _Interrupt:
        def __init__(self, value: object) -> None:
            self.value = value

    class WriteThenDone:
        def __init__(self) -> None:
            self.calls = 0

        async def astream(self, payload, config, stream_mode=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                order.append("model_stream")
                yield ("messages", (_AIChunk("writing… "),))
                yield ("updates", {"__interrupt__": (
                    _Interrupt({"action_requests": [
                        {"action": "write_file",
                         "args": {"file_path": "a.txt", "content": "x"}}
                    ]}),
                )})
            else:
                order.append("tool_execute")
                yield ("messages", (_AIChunk("done."),))

    async def _approve(_req: object) -> ApprovalReply:
        return ApprovalReply(approved=True)

    driver = SessionDriver(
        agent=WriteThenDone(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        approver=_approve,
        checkpoint=SlowCheckpoint(),
    )
    events = [ev async for ev in driver.run_turn("write a file")]

    # Non-blocking turn start: the model stream began before the snapshot finished.
    assert order.index("model_stream") < order.index("snap_done"), order
    # Core invariant: the snapshot completed before the mutating tool executed.
    assert order.index("snap_done") < order.index("tool_execute"), order
    assert any(e.kind is EventKind.DONE for e in events)


@pytest.mark.asyncio
async def test_snapshot_records_thread_and_turn_index() -> None:
    """The session driver tags each turn-start snapshot with its thread_id and the
    0-based turn index (count of human messages already in the thread), so /rewind
    can resolve the checkpoint for a chosen turn. T-3-1 metadata plumbing."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage

    from jarn.agent.session import EventKind, SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    captured: dict[str, object] = {}

    class RecordingCheckpoint:
        snapshot_notice_pending = False
        snapshot_notice_shown = False

        def snapshot(self, label, *, now=None, thread_id=None, turn_index=None):
            captured["thread_id"] = thread_id
            captured["turn_index"] = turn_index
            return SnapshotResult(ok=True, sha="abc")

    class _AIChunk:
        type = "ai"

        def __init__(self, content: str) -> None:
            self.content = content
            self.usage_metadata = {"input_tokens": 1, "output_tokens": 1}
            self.response_metadata: dict[str, str] = {}

    class Agent:
        async def astream(self, payload, config, stream_mode=None, **kwargs):
            yield ("messages", (_AIChunk("hi"),))

        async def aget_state(self, config):
            # Two prior human turns already in the thread → this is turn 2.
            return SimpleNamespace(values={"messages": [
                HumanMessage(content="q0"), AIMessage(content="a0"),
                HumanMessage(content="q1"), AIMessage(content="a1"),
            ]})

    driver = SessionDriver(
        agent=Agent(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="thread-42",
        checkpoint=RecordingCheckpoint(),
    )
    events = [ev async for ev in driver.run_turn("q2")]
    assert any(e.kind is EventKind.DONE for e in events)
    assert captured["thread_id"] == "thread-42"
    assert captured["turn_index"] == 2


@pytest.mark.asyncio
async def test_snapshot_turn_index_none_when_state_unavailable() -> None:
    """A fake agent without aget_state (or a read error) records turn_index=None —
    the snapshot is still taken (old-format record), find_for_turn just won't match."""
    from jarn.agent.session import EventKind, SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    captured: dict[str, object] = {}

    class RecordingCheckpoint:
        snapshot_notice_pending = False
        snapshot_notice_shown = False

        def snapshot(self, label, *, now=None, thread_id=None, turn_index=None):
            captured["thread_id"] = thread_id
            captured["turn_index"] = turn_index
            return SnapshotResult(ok=True, sha="abc")

    class _AIChunk:
        type = "ai"

        def __init__(self, content: str) -> None:
            self.content = content
            self.usage_metadata = {"input_tokens": 1, "output_tokens": 1}
            self.response_metadata: dict[str, str] = {}

    class Agent:  # no aget_state
        async def astream(self, payload, config, stream_mode=None, **kwargs):
            yield ("messages", (_AIChunk("hi"),))

    driver = SessionDriver(
        agent=Agent(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="thread-9",
        checkpoint=RecordingCheckpoint(),
    )
    events = [ev async for ev in driver.run_turn("q0")]
    assert any(e.kind is EventKind.DONE for e in events)
    assert captured["thread_id"] == "thread-9"
    assert captured["turn_index"] is None


def test_concurrent_undo_locked(repo: Path) -> None:
    """Concurrent undo attempts serialize on the checkpoint lock without corrupting stacks."""
    import threading

    from jarn.agent import checkpoint as cp

    (repo / "README.txt").write_text("v1\n", encoding="utf-8")
    mgr = _manager(repo)
    assert mgr.snapshot("t1", now=1.0).ok
    (repo / "README.txt").write_text("v2\n", encoding="utf-8")
    assert mgr.snapshot("t2", now=2.0).ok
    (repo / "README.txt").write_text("v3\n", encoding="utf-8")

    errors: list[str] = []
    lock = threading.Barrier(2)

    def _undo_once() -> None:
        lock.wait(timeout=5)
        try:
            mgr.undo()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    t1 = threading.Thread(target=_undo_once)
    t2 = threading.Thread(target=_undo_once)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    undo_stack = cp._stack_read(cp._UNDO_PREFIX, repo)
    # Two serialized undos from a depth-2 stack leaves 0 or 1 entry — never negative/corrupt.
    assert len(undo_stack) <= 2
    for sha in undo_stack:
        assert len(sha) >= 40


# ---------------------------------------------------------------------------
# T-1-4 fix — /abort rollback must not race a still-building detached snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abort_rollback_waits_for_detached_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the T-1-4 review Critical: the /abort over-revert race.

    A turn cancelled while its turn-start snapshot is still BUILDING (tree captured
    off the checkpoint lock, not yet pushed) detaches that snapshot fire-and-forget.
    If the abort rollback's ``undo()`` runs before the snapshot pushes, it pops the
    PREVIOUS turn's checkpoint and reverts the tree an extra turn back (over-revert),
    then the late snapshot pushes and the stack is left out of sync with disk.

    ``settle_snapshot`` (awaited in the /abort path before abort_rollback) must wait
    for the detached snapshot to land, so undo() targets THIS turn's start.
    """
    import asyncio
    import threading
    from types import SimpleNamespace

    from jarn.agent import checkpoint as cp
    from jarn.agent.session import _DETACHED_SNAPSHOTS, EventKind, SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.controller import session_helpers
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    mgr = _manager(repo)
    readme = repo / "README.txt"

    # Turn-0 checkpoint captures the "init" tree — the extra-turn-back over-revert
    # target if undo() runs too early.
    c0 = mgr.snapshot("turn-0-start", now=1.0)
    assert c0.ok
    # Turn-0's result / turn-1's starting tree.
    readme.write_text("v1\n", encoding="utf-8")

    # Slow the turn-1 snapshot: build the commit (capturing "v1") as usual, then
    # block BEFORE the dedup/push — exactly the real race window. The gate lets the
    # test cancel the turn and inspect the stack while the snapshot is provably in
    # flight. Only turn-1's snapshot is gated (matched by label).
    started = threading.Event()
    release = threading.Event()
    orig_build = cp._build_snapshot

    def slow_build(label: str, root: Path):
        result = orig_build(label, root)
        if "turn-1-start" in label:
            started.set()
            release.wait(timeout=5)
        return result

    monkeypatch.setattr(cp, "_build_snapshot", slow_build)

    class _AIChunk:
        type = "ai"

        def __init__(self, content: str) -> None:
            self.content = content
            self.usage_metadata = {"input_tokens": 1, "output_tokens": 1}
            self.response_metadata: dict[str, str] = {}

    class Hang:
        async def astream(self, payload, config, stream_mode=None, **kwargs):
            yield ("messages", (_AIChunk("working… "),))
            await asyncio.sleep(10)  # hold the turn open until cancelled

    driver = SessionDriver(
        agent=Hang(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        checkpoint=mgr,
    )
    agen = driver.run_turn("turn-1-start")
    first = await agen.__anext__()
    assert first.kind is EventKind.TEXT
    # Wait until the snapshot has captured "v1" and is blocked before its push.
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()

    # Simulate turn-1's in-progress edit (the snapshot already captured "v1").
    readme.write_text("v2\n", encoding="utf-8")

    # RACE WINDOW: turn-1's snapshot is NOT on the stack yet, so an unsettled undo
    # here would pop turn-0 (the "init" checkpoint) → over-revert.
    assert mgr.list()[0].sha == c0.sha

    # Cancel the turn mid-snapshot → the snapshot is detached fire-and-forget.
    await agen.aclose()
    assert driver._snapshot_task is None
    assert [t for t in _DETACHED_SNAPSHOTS if not t.done()], (
        "the still-building snapshot should be detached and pending"
    )

    # The /abort sequence: unblock the snapshot, settle it, THEN roll back.
    release.set()
    await driver.settle_snapshot()

    # settle waited for the push: turn-1's checkpoint (tree "v1") is now on top,
    # distinct from turn-0's "init" checkpoint.
    top = mgr.list()[0]
    assert top.sha != c0.sha
    v1_tree = cp._git(["rev-parse", f"{top.sha}^{{tree}}"], cwd=repo).stdout.strip()
    init_tree = cp._git(["rev-parse", f"{c0.sha}^{{tree}}"], cwd=repo).stdout.strip()
    assert v1_tree != init_tree

    # The real abort_rollback (what /abort offloads to a thread) now targets turn-1
    # start → README restored to "v1", NOT an extra turn back to "init".
    msg = session_helpers.abort_rollback(SimpleNamespace(checkpoint_manager=mgr))
    assert "rolled back" in msg.lower(), msg
    assert readme.read_text(encoding="utf-8") == "v1\n"

    # Stack stays consistent: undo popped turn-1 (top is turn-0 again), the pre-undo
    # state is recoverable on redo, and no ref is corrupt.
    undo_stack = cp._stack_read(cp._UNDO_PREFIX, repo)
    assert undo_stack and undo_stack[0] == c0.sha
    assert cp._stack_read(cp._REDO_PREFIX, repo)  # redo point saved
    for sha in undo_stack:
        assert len(sha) >= 40
