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
