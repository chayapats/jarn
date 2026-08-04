"""Filesystem path boundary regressions."""

import os
import stat
from pathlib import Path

import pytest

from jarn.config import paths


def _use_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("JARN_HOME", raising=False)


def test_global_jarn_home_is_not_a_project_marker(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    cwd = home / "Documents" / "scratch"
    cwd.mkdir(parents=True)
    (home / ".jarn").mkdir()
    _use_home(monkeypatch, home)
    monkeypatch.chdir(cwd)

    assert paths.find_project_root() is None
    assert paths.project_dir() is None
    assert paths.project_config_path() is None


def test_default_global_home_is_reserved_when_jarn_home_is_overridden(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    cwd = home / "work" / "scratch"
    cwd.mkdir(parents=True)
    (home / ".jarn").mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "custom-jarn-home"))

    assert paths.find_project_root(cwd) is None
    assert paths.project_dir(home) is None


def test_home_git_project_keeps_root_without_global_tier_collision(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    cwd = home / "src" / "package"
    cwd.mkdir(parents=True)
    (home / ".jarn").mkdir()
    (home / ".git").mkdir()
    _use_home(monkeypatch, home)

    assert paths.find_project_root(cwd) == home
    assert paths.project_dir(home) is None
    assert paths.project_config_path(home) is None
    assert paths.project_claude_dir(home) is None


def test_project_root_search_stops_at_home(tmp_path, monkeypatch) -> None:
    """A stray marker above $HOME must never become the project root.

    Ignoring the global ``~/.jarn`` marker removed what used to terminate the
    upward walk. The root this returns becomes an in-scope write root for the
    permission engine, so an escape puts unrelated trees — up to all of ``/`` —
    inside the project boundary.
    """
    above = tmp_path / "above"
    home = above / "home"
    cwd = home / "notes"
    cwd.mkdir(parents=True)
    (home / ".jarn").mkdir()  # the global tier — correctly ignored
    (above / ".git").mkdir()  # a stray marker the walk must not reach
    _use_home(monkeypatch, home)

    assert paths.find_project_root(cwd) is None


def test_paths_work_when_the_host_has_no_home_directory(tmp_path, monkeypatch) -> None:
    """An explicit JARN_HOME must survive a host where ``Path.home()`` raises.

    ``action/action.yml`` sets ``JARN_HOME`` precisely because the runner may
    have no usable ``$HOME``; resolving the default home unconditionally took the
    whole path layer down with it even though the override was present.
    """

    def _no_home():
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", _no_home)
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "jarn-home"))

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    assert paths.global_home() == tmp_path / "jarn-home"
    assert paths.jarn_home_overridden() is True
    assert paths.find_project_root(repo) == repo
    assert paths.project_dir(repo) == repo / ".jarn"
    assert paths.project_config_path(repo) == repo / ".jarn" / "config.yaml"
    assert paths.project_claude_dir(repo) == repo / ".claude"


# -- Global-home permissions (GHSA-hqcx-wg2w-6gv4) ---------------------------
#
# `~/.jarn` was created with no `mode=`, so it took the process umask — 0755 on a
# stock macOS install — and jarn never tightened it. Every other local account
# could read the prompt history, session transcripts, the wiki and memory the
# agent writes for itself, and the trust store (which repos the operator has
# trusted). Negligible on a single-user laptop; not on the always-on VPS
# appliance jarn is meant to run as.


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_fresh_global_home_is_owner_only(tmp_path, monkeypatch):
    """Created 0700 OUTRIGHT, not created at the umask and tightened afterwards.

    The create-time mode is the half the chmod cannot cover: between the two there
    is a window in which the directory exists at the umask, and a chmod that is
    doing all the work would hide a missing `mode=`. The umask is pinned so the
    assertion does not depend on the developer's ambient value, and the chmod is a
    tripwire rather than the mechanism under test.
    """
    home = tmp_path / "jarn-home"
    monkeypatch.setenv("JARN_HOME", str(home))
    repaired: list[int] = []
    real_chmod = Path.chmod
    monkeypatch.setattr(
        Path, "chmod", lambda self, m, *a, **k: (repaired.append(m), real_chmod(self, m, *a, **k))[1]
    )

    old_umask = os.umask(0o022)
    try:
        assert paths.ensure_global_home() == home
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert repaired == [], "a fresh home must be created 0700, not chmod'ed into it"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_an_existing_world_readable_home_is_repaired(tmp_path, monkeypatch):
    """`mkdir(mode=…)` is masked by the umask AND a no-op for an existing
    directory, so only the explicit chmod fixes installs already in the field."""
    home = tmp_path / "jarn-home"
    home.mkdir()
    home.chmod(0o755)
    (home / "history").write_text("every prompt ever typed", encoding="utf-8")
    monkeypatch.setenv("JARN_HOME", str(home))

    paths.ensure_global_home()

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    # Repairing permissions must not disturb the contents.
    assert (home / "history").read_text(encoding="utf-8") == "every prompt ever typed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_ensure_global_home_only_tightens(tmp_path, monkeypatch):
    """An already-restrictive home is left exactly as the operator set it."""
    home = tmp_path / "jarn-home"
    home.mkdir(mode=0o700)
    home.chmod(0o500)  # stricter than we would set
    monkeypatch.setenv("JARN_HOME", str(home))

    paths.ensure_global_home()

    assert stat.S_IMODE(home.stat().st_mode) == 0o500


def test_ensure_global_home_never_raises_on_an_unwritable_parent(tmp_path, monkeypatch):
    """A missing or read-only home is a real configuration and must not be what
    takes the process down — the same discipline as commit 0b547ac."""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    monkeypatch.setenv("JARN_HOME", str(readonly / "sub" / "jarn-home"))
    try:
        assert paths.ensure_global_home() is not None  # returns the path, no raise
    finally:
        readonly.chmod(0o700)


def test_ensure_global_home_returns_none_when_the_host_has_no_home(monkeypatch):
    """`Path.home()` raises when $HOME is unset and the uid has no passwd entry."""
    def _no_home():
        raise RuntimeError("Could not determine home directory")

    monkeypatch.delenv("JARN_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", _no_home)
    assert paths.ensure_global_home() is None
