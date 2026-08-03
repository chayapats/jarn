"""Filesystem path boundary regressions."""

from pathlib import Path

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
