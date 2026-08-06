"""Per-root flock lease: first holder wins; contended acquire fails (no queue)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarn.gateway.lease import (
    LOCK_FILENAME,
    RootLease,
    RootLeaseHeldError,
    lock_path_for,
)


def test_lock_path_is_under_project_jarn(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert lock_path_for(root) == (root / ".jarn" / LOCK_FILENAME).resolve()


def test_second_acquire_on_same_root_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    first = RootLease(root)
    first.acquire()
    try:
        assert first.held
        assert (root / ".jarn" / LOCK_FILENAME).is_file()
        with pytest.raises(RootLeaseHeldError) as excinfo:
            RootLease(root).acquire()
        assert excinfo.value.root == root.resolve()
        assert "lease held" in str(excinfo.value)
    finally:
        first.release()

    # After release, a new holder can take it.
    with RootLease(root) as lease:
        assert lease.held


def test_context_manager_releases_on_exit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    with RootLease(root):
        with pytest.raises(RootLeaseHeldError):
            with RootLease(root):
                pass

    with RootLease(root):
        pass  # must succeed after the previous block exited


def test_different_roots_do_not_contend(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    with RootLease(a) as lease_a, RootLease(b) as lease_b:
        assert lease_a.held and lease_b.held
        assert lock_path_for(a) != lock_path_for(b)


def test_release_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    lease = RootLease(root)
    lease.acquire()
    lease.release()
    lease.release()
    assert not lease.held
