"""Auto-checkpoint manager — reversible agent edits via git plumbing.

Snapshots the working tree (tracked + untracked files) under a private ref
namespace (``refs/jarn/checkpoints/``) without touching HEAD, the branch, or
the staged index. This lets the user undo the last agent turn with ``/undo``
and re-apply with ``/redo``.

Safety invariants (enforced by design, proven by tests):

1. ``HEAD``, the branch, and the staging index are NEVER modified.  We use a
   *temporary* ``GIT_INDEX_FILE`` for building the snapshot tree; the real
   index is never read or written except to capture it as part of the snapshot.
2. Snapshots capture BOTH tracked modifications AND newly created untracked
   files (``git add -A`` into the temp index).
3. ``undo()`` saves the current state as a redo-point BEFORE restoring, so
   undo is itself reversible and no uncommitted work is ever lost.
4. Every operation is a no-op (returns a clear message, no exception) when:
   the feature is disabled, the directory is not a git repo, or there is
   nothing new to snapshot.
5. If any git subprocess fails, a clear message is returned and the working
   tree is left untouched — never half-applied.

Restore mechanics (chosen for correctness over brevity):

* We restore via ``git restore --source=<sha> --worktree -- .`` which sets all
  tracked files to the snapshot state without touching the index or HEAD.
* Files that exist in the current tree but were absent in the snapshot (i.e.
  files the agent *created*) are removed individually.  We deliberately avoid
  a blind ``git clean`` so we never accidentally wipe files that belong to the
  user and were merely untracked before the snapshot.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
import time as _time_module
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

#: Full snapshot ref suffix — avoids prefix collisions on large repos.
_SNAP_REF_LEN = 40

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SnapshotResult:
    """Outcome of a ``snapshot()`` call."""
    ok: bool
    sha: str = ""          # the commit SHA under refs/jarn/checkpoints/...
    message: str = ""      # human-readable status / error


@dataclass(slots=True)
class RestoreResult:
    """Outcome of an ``undo()`` or ``redo()`` call."""
    ok: bool
    message: str = ""


@dataclass(slots=True)
class CheckpointEntry:
    """A single checkpoint in the stack."""
    sha: str
    label: str
    ref: str               # full ref name, e.g. refs/jarn/checkpoints/undo/0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``cwd``.  Never raises; caller checks returncode."""
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=base_env,
        capture_output=capture,
        text=True,
    )


def _is_git_repo(root: Path) -> bool:
    """Return True if ``root`` is inside a git work-tree."""
    result = _git(["rev-parse", "--is-inside-work-tree"], cwd=root)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _get_head_sha(root: Path) -> str | None:
    """Return the current HEAD commit SHA, or None if repo has no commits."""
    result = _git(["rev-parse", "HEAD"], cwd=root)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _list_tree_paths(sha: str, root: Path) -> set[str]:
    """Return the set of file paths recorded in the tree of commit ``sha``."""
    result = _git(["ls-tree", "-r", "--name-only", sha], cwd=root)
    if result.returncode != 0:
        return set()
    return {line for line in result.stdout.splitlines() if line}


def _ref_exists(ref: str, root: Path) -> bool:
    result = _git(["rev-parse", "--verify", ref], cwd=root)
    return result.returncode == 0


def _read_ref(ref: str, root: Path) -> str | None:
    result = _git(["rev-parse", ref], cwd=root)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _update_ref(ref: str, sha: str, root: Path) -> bool:
    """Set ``ref`` to ``sha``.  Returns True on success."""
    result = _git(["update-ref", ref, sha], cwd=root)
    return result.returncode == 0


def _delete_ref(ref: str, root: Path) -> bool:
    result = _git(["update-ref", "-d", ref], cwd=root)
    return result.returncode == 0


def _read_ref_message(sha: str, root: Path) -> str:
    """Return the commit subject line for a snapshot SHA."""
    result = _git(["log", "-1", "--pretty=%s", sha], cwd=root)
    if result.returncode != 0:
        return sha[:12]
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Stack persistence helpers
#
# The undo/redo stacks are maintained as a pair of simple git refs:
#   refs/jarn/checkpoints/stack/undo/<N>   (N = 0, 1, 2, …; 0 is the top)
#   refs/jarn/checkpoints/stack/redo/<N>
#
# This keeps everything inside git so there's no external state file to
# maintain or lose-sync with.  We iterate forward from N=0 until a ref is
# missing to find the stack size.
# ---------------------------------------------------------------------------

_UNDO_PREFIX = "refs/jarn/checkpoints/stack/undo/"
_REDO_PREFIX = "refs/jarn/checkpoints/stack/redo/"
_LOCK_NAME = ".jarn-checkpoint.lock"
# In-process guard paired with the file lock (same-thread reentrancy).
_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _checkpoint_lock(root: Path):
    """Serialize stack mutations across threads and cooperating processes."""
    lock_path = root / _LOCK_NAME
    with _THREAD_LOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _snap_ref(sha: str) -> str:
    """Stable private ref name for a snapshot commit."""
    return f"refs/jarn/checkpoints/snap/{sha[:_SNAP_REF_LEN]}"


def _stack_push(prefix: str, sha: str, root: Path) -> bool:
    """Prepend ``sha`` to the stack at ``prefix`` (shift existing entries down).

    We re-index 0→1, 1→2, … *before* writing the new 0 so the operation is
    atomic-ish: a crash after shifting but before writing 0 leaves the stack
    with a gap at 0, which ``_stack_read`` tolerates by stopping at the first
    missing entry.
    """
    # Collect current stack
    entries = _stack_read(prefix, root)
    # Shift existing entries one position deeper (highest index first, to avoid
    # overwriting a ref we haven't shifted yet).
    for i in range(len(entries) - 1, -1, -1):
        old_ref = f"{prefix}{i}"
        new_ref = f"{prefix}{i + 1}"
        old_sha = _read_ref(old_ref, root)
        if old_sha:
            _update_ref(new_ref, old_sha, root)
    # Write the new top entry.
    ok = _update_ref(f"{prefix}0", sha, root)
    return ok


def _stack_pop(prefix: str, root: Path) -> str | None:
    """Remove and return the top SHA from ``prefix``, shifting remaining down."""
    entries = _stack_read(prefix, root)
    if not entries:
        return None
    top_sha = entries[0]
    # Delete the top ref.
    _delete_ref(f"{prefix}0", root)
    # Shift remaining entries up.
    for i in range(1, len(entries)):
        old_ref = f"{prefix}{i}"
        new_ref = f"{prefix}{i - 1}"
        sha = _read_ref(old_ref, root)
        if sha:
            _update_ref(new_ref, sha, root)
        _delete_ref(old_ref, root)
    return top_sha


def _stack_read(prefix: str, root: Path) -> list[str]:
    """Return the stack as an ordered list (index 0 = top)."""
    entries: list[str] = []
    i = 0
    while True:
        ref = f"{prefix}{i}"
        sha = _read_ref(ref, root)
        if sha is None:
            break
        entries.append(sha)
        i += 1
    return entries


def _stack_clear(prefix: str, root: Path) -> None:
    """Remove all entries in a stack prefix."""
    i = 0
    while True:
        ref = f"{prefix}{i}"
        if not _ref_exists(ref, root):
            break
        _delete_ref(ref, root)
        i += 1


# ---------------------------------------------------------------------------
# Core snapshot / restore logic
# ---------------------------------------------------------------------------

def _build_snapshot(label: str, root: Path) -> tuple[str, str] | tuple[None, str]:
    """Build a snapshot commit from the current working tree using a temp index.

    Returns ``(sha, "")`` on success or ``(None, error_message)`` on failure.

    The real git index is never touched; we use ``GIT_INDEX_FILE`` to redirect
    all index operations to a temporary file and remove it afterwards.
    """
    head_sha = _get_head_sha(root)
    if head_sha is None:
        # Repo has no commits yet — can't use read-tree.
        return None, "repo has no commits yet; skipping snapshot"

    with tempfile.NamedTemporaryFile(
        prefix="jarn-idx-", suffix=".idx", delete=False
    ) as tmp_f:
        tmp_idx = tmp_f.name

    try:
        idx_env = {"GIT_INDEX_FILE": tmp_idx}

        # Seed the temp index from HEAD so that tracked deletions are recorded.
        r = _git(["read-tree", "HEAD"], cwd=root, env=idx_env)
        if r.returncode != 0:
            return None, f"read-tree failed: {r.stderr.strip()}"

        # Stage all changes (tracked + untracked) into the temp index.
        # ``-A`` includes new untracked files and records deletions of tracked
        # files. ``.gitignore`` is honoured (``git add`` respects it).
        r = _git(["add", "-A"], cwd=root, env=idx_env)
        if r.returncode != 0:
            return None, f"git add -A failed: {r.stderr.strip()}"

        # Write the tree.
        r = _git(["write-tree"], cwd=root, env=idx_env)
        if r.returncode != 0:
            return None, f"write-tree failed: {r.stderr.strip()}"
        tree_sha = r.stdout.strip()

        # Create the commit without touching HEAD or the branch.
        r = _git(
            ["commit-tree", tree_sha, "-p", head_sha, "-m", label],
            cwd=root,
        )
        if r.returncode != 0:
            return None, f"commit-tree failed: {r.stderr.strip()}"
        snapshot_sha = r.stdout.strip()

        return snapshot_sha, ""
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_idx)


def _apply_snapshot(sha: str, root: Path) -> tuple[bool, str]:
    """Restore the working tree to the state captured in snapshot commit ``sha``.

    Steps:
    1. Identify files present NOW but absent in ``sha`` (agent-added files);
       remove them after a successful restore.
    2. Run ``git restore --source=<sha> --worktree -- .`` to set all tracked
       files to the snapshot state without touching HEAD or the index.
    3. Delete any files that were absent in ``sha``.

    Returns ``(True, "")`` on success or ``(False, error_message)`` on failure.
    HEAD and the staged index are unchanged on both success and failure.
    """
    # -- files present in snapshot tree ---
    snapshot_paths = _list_tree_paths(sha, root)

    # -- files present in current worktree (tracked by git) ---
    current_r = _git(["ls-files"], cwd=root)
    if current_r.returncode != 0:
        return False, f"ls-files failed: {current_r.stderr.strip()}"
    current_paths = {line for line in current_r.stdout.splitlines() if line}

    # -- untracked files in the working tree ---
    # We also need to remove untracked files that the agent added (they would
    # not appear in ls-files but are present on disk).
    untracked_r = _git(["ls-files", "--others", "--exclude-standard"], cwd=root)
    untracked_paths = set()
    if untracked_r.returncode == 0:
        untracked_paths = {line for line in untracked_r.stdout.splitlines() if line}

    # Files to remove = files in (current tracked + current untracked) that are
    # absent in the snapshot.  We only remove files under the repo root.
    to_remove: set[str] = (current_paths | untracked_paths) - snapshot_paths

    # -- restore tracked files --
    # ``git restore --source=<sha> --worktree -- .`` sets each tracked file in
    # the worktree to match the snapshot without modifying HEAD or the index.
    r = _git(
        ["restore", f"--source={sha}", "--worktree", "--", "."],
        cwd=root,
    )
    if r.returncode != 0:
        return False, f"git restore failed: {r.stderr.strip()}"

    # -- restore deleted files: files in snapshot that aren't in current ---
    # git restore handles this: if a path exists in <sha> but is absent on
    # disk, restore re-creates it.  Nothing extra needed.

    # -- remove agent-added files --
    for rel_path in sorted(to_remove):
        abs_path = root / rel_path
        try:
            if abs_path.is_file() or abs_path.is_symlink():
                abs_path.unlink()
            elif abs_path.is_dir():
                import shutil
                shutil.rmtree(abs_path)
        except OSError:
            # Best-effort; continue cleaning up the rest.
            pass

    return True, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class CheckpointManager:
    """Manages auto-checkpoints for a git working tree.

    All git operations go via subprocess with an explicit ``cwd``; we never
    shell through the agent or import git libraries — this keeps the critical
    path minimal and auditable.

    The undo/redo stacks are stored under ``refs/jarn/checkpoints/stack/``
    which are private refs (not branches/tags) so they never interfere with the
    user's branch or push to remotes unless the user explicitly fetches those
    refs.
    """

    repo_root: Path
    enabled: bool = True
    # Max depth of each stack (undo / redo).  Older entries are pruned.
    max_stack: int = 20
    _is_repo: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        # Cache the repo check once at construction; no subprocess per-call.
        self._is_repo = _is_git_repo(self.repo_root)

    @property
    def is_repo(self) -> bool:
        """Whether ``repo_root`` is inside a git repository (cached at init)."""
        return self._is_repo

    # -- primary interface --------------------------------------------------

    def snapshot(
        self,
        label: str,
        *,
        now: float | None = None,
    ) -> SnapshotResult:
        """Capture the current working tree into the undo stack.

        Returns a :class:`SnapshotResult` with ``ok=True`` and the SHA if the
        snapshot was stored, or ``ok=False`` with a human-readable ``message``
        explaining why it was skipped (disabled, not a repo, no commits, etc.).

        Pushing a new snapshot clears the redo stack (like most undo systems).
        ``now`` is the current epoch time; injectable for testing.
        """
        if not self.enabled:
            return SnapshotResult(ok=False, message="autocheckpoint disabled")
        if not self._is_repo:
            return SnapshotResult(ok=False, message="not a git repository")

        ts = now if now is not None else _time_module.time()
        full_label = f"jarn-checkpoint: {label} @ {int(ts)}"

        sha, err = _build_snapshot(full_label, self.repo_root)
        if sha is None:
            return SnapshotResult(ok=False, message=err)

        # Deduplication guard: if the top of the undo stack already captures
        # exactly this tree, don't push a duplicate entry (e.g. /undo called
        # on an already-clean tree).  We compare trees, not commit SHAs, because
        # each snapshot commit has a unique timestamp in its message.
        undo_stack = _stack_read(_UNDO_PREFIX, self.repo_root)
        if undo_stack:
            top_sha = undo_stack[0]
            top_tree_r = _git(
                ["rev-parse", f"{top_sha}^{{tree}}"], cwd=self.repo_root
            )
            snap_tree_r = _git(
                ["rev-parse", f"{sha}^{{tree}}"], cwd=self.repo_root
            )
            if (
                top_tree_r.returncode == 0
                and snap_tree_r.returncode == 0
                and top_tree_r.stdout.strip() == snap_tree_r.stdout.strip()
            ):
                return SnapshotResult(
                    ok=False,
                    message="nothing to snapshot (working tree already saved)",
                )

        with _checkpoint_lock(self.repo_root):
            # Store the snapshot SHA under a stable ref so it won't be GC'd.
            ref = _snap_ref(sha)
            if not _update_ref(ref, sha, self.repo_root):
                return SnapshotResult(ok=False, message="failed to store snapshot ref")

            # Push onto the undo stack.
            if not _stack_push(_UNDO_PREFIX, sha, self.repo_root):
                return SnapshotResult(ok=False, message="failed to update undo stack")

            # Clear redo: a new snapshot invalidates any pending redo points.
            _stack_clear(_REDO_PREFIX, self.repo_root)

            # Prune oversized stacks.
            self._prune_stack(_UNDO_PREFIX)

        return SnapshotResult(ok=True, sha=sha, message=full_label)

    def undo(self) -> RestoreResult:
        """Restore the working tree to the most recent undo snapshot.

        Before restoring, captures the current state as a redo-point so that
        the operation is always reversible and no uncommitted work is lost.

        Returns a descriptive :class:`RestoreResult` for the UI.
        """
        if not self.enabled:
            return RestoreResult(ok=False, message="autocheckpoint disabled")
        if not self._is_repo:
            return RestoreResult(ok=False, message="not a git repository")

        with _checkpoint_lock(self.repo_root):
            undo_stack = _stack_read(_UNDO_PREFIX, self.repo_root)
            if not undo_stack:
                return RestoreResult(ok=False, message="nothing to undo")

            # Build the redo-point snapshot but defer the stack push until apply
            # succeeds — otherwise a failed restore leaves an orphan on redo.
            redo_result = self._build_redo_point()
            if not redo_result.ok:
                return RestoreResult(
                    ok=False,
                    message=f"could not save redo point: {redo_result.message}",
                )

            target_sha = _stack_pop(_UNDO_PREFIX, self.repo_root)
            if target_sha is None:
                return RestoreResult(ok=False, message="undo stack is empty")

            ok, err = _apply_snapshot(target_sha, self.repo_root)
            if not ok:
                _stack_push(_UNDO_PREFIX, target_sha, self.repo_root)
                return RestoreResult(ok=False, message=f"restore failed: {err}")

            if redo_result.sha:
                _update_ref(_snap_ref(redo_result.sha), redo_result.sha, self.repo_root)
                _stack_push(_REDO_PREFIX, redo_result.sha, self.repo_root)
                self._prune_stack(_REDO_PREFIX)

        label = _read_ref_message(target_sha, self.repo_root)
        return RestoreResult(ok=True, message=f"undone: {label}")

    def redo(self) -> RestoreResult:
        """Re-apply the most recent redo snapshot (inverses /undo).

        Before re-applying, captures the current state back onto the undo stack
        so the operation is again reversible.
        """
        if not self.enabled:
            return RestoreResult(ok=False, message="autocheckpoint disabled")
        if not self._is_repo:
            return RestoreResult(ok=False, message="not a git repository")

        with _checkpoint_lock(self.repo_root):
            redo_stack = _stack_read(_REDO_PREFIX, self.repo_root)
            if not redo_stack:
                return RestoreResult(ok=False, message="nothing to redo")

            ts = _time_module.time()
            pre_sha, err = _build_snapshot(
                f"jarn-checkpoint: pre-redo @ {int(ts)}", self.repo_root
            )
            if pre_sha is None:
                return RestoreResult(
                    ok=False, message=f"could not save pre-redo snapshot: {err}"
                )

            target_sha = _stack_pop(_REDO_PREFIX, self.repo_root)
            if target_sha is None:
                return RestoreResult(ok=False, message="redo stack is empty")

            ok, err = _apply_snapshot(target_sha, self.repo_root)
            if not ok:
                _stack_push(_REDO_PREFIX, target_sha, self.repo_root)
                return RestoreResult(ok=False, message=f"redo failed: {err}")

            _update_ref(_snap_ref(pre_sha), pre_sha, self.repo_root)
            _stack_push(_UNDO_PREFIX, pre_sha, self.repo_root)
            self._prune_stack(_UNDO_PREFIX)

        label = _read_ref_message(target_sha, self.repo_root)
        return RestoreResult(ok=True, message=f"redone: {label}")

    def list(self) -> list[CheckpointEntry]:
        """Return the undo stack as an ordered list (most recent first)."""
        if not self._is_repo:
            return []
        entries: list[CheckpointEntry] = []
        for i, sha in enumerate(_stack_read(_UNDO_PREFIX, self.repo_root)):
            ref = f"{_UNDO_PREFIX}{i}"
            label = _read_ref_message(sha, self.repo_root)
            entries.append(CheckpointEntry(sha=sha, label=label, ref=ref))
        return entries

    # -- internals ----------------------------------------------------------

    def _build_redo_point(self) -> SnapshotResult:
        """Build a redo-point snapshot without mutating the redo stack."""
        ts = _time_module.time()
        sha, err = _build_snapshot(
            f"jarn-checkpoint: pre-undo @ {int(ts)}", self.repo_root
        )
        if sha is None:
            if "no commits" in err or "nothing to" in err:
                return SnapshotResult(ok=True, sha="", message=err)
            return SnapshotResult(ok=False, message=err)
        return SnapshotResult(ok=True, sha=sha)

    def _capture_redo_point(self) -> SnapshotResult:
        """Snapshot the current state onto the redo stack (called inside undo)."""
        result = self._build_redo_point()
        if not result.ok or not result.sha:
            return result
        _update_ref(_snap_ref(result.sha), result.sha, self.repo_root)
        _stack_push(_REDO_PREFIX, result.sha, self.repo_root)
        self._prune_stack(_REDO_PREFIX)
        return result

    def _prune_stack(self, prefix: str) -> None:
        """Remove stack entries beyond ``max_stack``."""
        i = self.max_stack
        while _ref_exists(f"{prefix}{i}", self.repo_root):
            _delete_ref(f"{prefix}{i}", self.repo_root)
            i += 1
