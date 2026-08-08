"""The shared write primitive: atomic publication + cross-process write locks.

These are the two halves behind the concurrency losses measured in the stores that
use them (see ``test_concurrent_stores``). Tested here in isolation so a regression
points at the primitive rather than at whichever caller noticed first.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarn.util.atomic import LOCK_SUFFIX, atomic_write_text, file_lock, lock_path_for


def test_publishes_content(tmp_path):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_creates_missing_parents(tmp_path):
    target = tmp_path / "a" / "b" / "out.txt"
    atomic_write_text(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_temp_name_is_unique_per_call(tmp_path, monkeypatch):
    """A FIXED `<path>.tmp` is the defect: two writers renamed each other's
    half-written file over the target and raised FileNotFoundError."""
    seen: list[str] = []
    real = Path.write_text

    def spy(self, *a, **kw):
        if self.name.endswith(".tmp"):
            seen.append(self.name)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", spy)
    target = tmp_path / "out.txt"
    for _ in range(5):
        atomic_write_text(target, "x")
    assert len(seen) == 5
    assert len(set(seen)) == 5, f"temp names collided: {seen}"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod only sets the read-only bit")
def test_mode_is_applied_before_the_rename(tmp_path, monkeypatch):
    """A secret written then chmod'ed is world-readable in between. The mode must
    be on the temp file, so the published file is never briefly at the umask."""
    observed: list[int] = []
    real = os.replace

    def spy(src, dst):
        observed.append(Path(src).stat().st_mode & 0o777)
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    target = tmp_path / "key"
    atomic_write_text(target, "gsk_secret", mode=0o600)
    assert observed == [0o600]
    assert target.stat().st_mode & 0o777 == 0o600


def test_failed_write_leaves_original_intact_and_no_debris(tmp_path, monkeypatch):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "original")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "replacement")
    assert target.read_text(encoding="utf-8") == "original"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_readers_never_see_a_partial_file(tmp_path):
    """os.replace is atomic, so a concurrent reader gets the old or the new bytes
    — never a truncated one. A bare write_text served an EMPTY index 173 times in
    the same shape of test."""
    target = tmp_path / "index.md"
    atomic_write_text(target, "A" * 4096)
    stop = threading.Event()
    bad: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                text = target.read_text(encoding="utf-8")
            except FileNotFoundError:
                bad.append("missing")
                continue
            # No `if text` guard: an EMPTY read is the exact failure this test
            # names (write_text truncates before it writes), and short-circuiting
            # on it is what made the original version of this test vacuous.
            if set(text) not in ({"A"}, {"B"}) or len(text) != 4096:
                bad.append(f"torn: {len(text)} bytes")

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    for i in range(200):
        atomic_write_text(target, ("A" if i % 2 else "B") * 4096)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert bad == []


def test_lock_path_is_a_sibling(tmp_path):
    target = tmp_path / "config.yaml"
    assert lock_path_for(target) == tmp_path / ("config.yaml" + LOCK_SUFFIX)


def test_lock_serializes_concurrent_holders(tmp_path):
    target = tmp_path / "config.yaml"
    overlaps = [0]
    inside = [0]
    guard = threading.Lock()

    def worker():
        with file_lock(target) as held:
            assert held is True
            with guard:
                inside[0] += 1
                if inside[0] > 1:
                    overlaps[0] += 1
            time.sleep(0.005)
            with guard:
                inside[0] -= 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps[0] == 0


def test_windows_lock_retry_rewinds_to_the_same_byte(tmp_path, monkeypatch):
    """Every Windows retry must target byte zero, not the descriptor's offset
    after the previous failed ``_locking`` call.

    Locking a later byte succeeds while another writer owns byte zero, so a
    drifting retry silently admits overlapping read-modify-write sections.  The
    fake CRT deliberately moves the file pointer on its first failure to make
    this otherwise timing-dependent Windows race deterministic on every OS.
    """
    from jarn.util import atomic

    target = tmp_path / "config.yaml.lock"
    target.write_bytes(b"\0")
    starts: list[int] = []

    with target.open("r+b") as handle:
        def locking(_fd: int, _mode: int, _nbytes: int) -> None:
            starts.append(handle.tell())
            if len(starts) == 1:
                handle.seek(1)
                raise OSError("byte zero is busy")

        fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=locking)
        monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
        monkeypatch.setattr(atomic.os, "name", "nt")
        monkeypatch.setattr(atomic, "_WINDOWS_RETRY_SECS", 0)

        atomic._acquire(handle)  # noqa: SLF001 - direct primitive regression

    assert starts == [0, 0]


def test_lock_degrades_to_unlocked_when_it_cannot_be_created(tmp_path, monkeypatch):
    """A missing or read-only home is a real configuration. Failing to lock must
    not take the process down — the caller proceeds, degraded, not broken."""
    def no_dir(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", no_dir)
    with file_lock(tmp_path / "nope" / "config.yaml") as held:
        assert held is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock path")
def test_lock_degrades_when_the_filesystem_refuses_to_lock(tmp_path, monkeypatch):
    """flock itself fails on mounts that do not implement it (ENOLCK on some
    NFS/FUSE). Guarding only open() left that escaping into callers — including
    MemoryStore._rebuild_index, which runs on the prompt-assembly path and never
    raised before."""
    import errno
    import fcntl

    def refuse(*a, **kw):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", refuse)
    with file_lock(tmp_path / "config.yaml") as held:
        assert held is False


def test_a_write_failure_inside_the_lock_still_propagates(tmp_path, monkeypatch):
    """The degrade guard must cover ACQUISITION only. If it wrapped the body, a
    real ENOSPC from the caller's write would become a silent no-op."""
    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError), file_lock(tmp_path / "config.yaml"):
        atomic_write_text(tmp_path / "config.yaml", "x")


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod only sets the read-only bit")
def test_existing_file_mode_is_carried_over(tmp_path):
    """Publishing by rename installs a NEW inode, so unlike write_text it replaces
    the target's mode. A user's `chmod 600` on a memory page must survive a save."""
    target = tmp_path / "note.md"
    atomic_write_text(target, "v1")
    target.chmod(0o600)
    atomic_write_text(target, "v2")
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text(encoding="utf-8") == "v2"


def test_publisher_takes_no_lock_so_a_store_can_wrap_one(tmp_path):
    """POSIX flock is per open file description: a second open() of the same lock
    file from the SAME process blocks against the first. If atomic_write_text
    locked internally, every store that wraps its read-modify-write in file_lock
    would deadlock. This is the regression test for that contract."""
    target = tmp_path / "config.yaml"
    done = threading.Event()

    def body():
        with file_lock(target):
            atomic_write_text(target, "written under the caller's lock")
        done.set()

    t = threading.Thread(target=body, daemon=True)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), "atomic_write_text deadlocked against the caller's lock"
    assert target.read_text(encoding="utf-8") == "written under the caller's lock"


def test_replace_is_retried_while_a_reader_holds_the_destination(tmp_path, monkeypatch):
    """Windows refuses `os.replace` with ERROR_ACCESS_DENIED while ANY process has
    the destination open — even for reading. POSIX has no such rule, so without a
    retry the publish that fixes torn reads everywhere else instead makes the
    WRITE fail on Windows, exactly under the concurrency it exists for.

    Simulated: this path only runs on Windows, so it cannot be exercised directly
    from a POSIX CI leg.
    """
    from jarn.util import atomic

    monkeypatch.setattr(atomic.os, "name", "nt")
    monkeypatch.setattr(atomic, "_REPLACE_RETRY_SECS", 0)
    real = os.replace
    calls = [0]

    def flaky(src, dst):
        calls[0] += 1
        if calls[0] < 3:  # a reader still has it open
            raise PermissionError(13, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(atomic.os, "replace", flaky)
    target = tmp_path / "index.md"
    atomic_write_text(target, "published")

    assert calls[0] == 3
    assert target.read_text(encoding="utf-8") == "published"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_replace_gives_up_rather_than_writing_in_place(tmp_path, monkeypatch):
    """Exhausting the retry deadline must RAISE, not fall back to a truncating
    write — silently reintroducing the torn read would undo the whole fix."""
    from jarn.util import atomic

    monkeypatch.setattr(atomic.os, "name", "nt")
    monkeypatch.setattr(atomic, "_REPLACE_RETRY_SECS", 0)
    monkeypatch.setattr(atomic, "_REPLACE_DEADLINE_SECS", 0.05)

    target = tmp_path / "index.md"
    atomic_write_text(target, "original")  # seeded before the destination "locks"

    def always_locked(src, dst):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(atomic.os, "replace", always_locked)

    with pytest.raises(PermissionError):
        atomic_write_text(target, "replacement")
    assert target.read_text(encoding="utf-8") == "original"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())
