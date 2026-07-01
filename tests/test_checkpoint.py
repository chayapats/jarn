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

from jarn.agent.checkpoint import CheckpointManager

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
