"""CLI subcommand tests (non-interactive paths)."""

from __future__ import annotations

from jarn.cli import main


def test_init_creates_jarn_md(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "Created" in out
    assert (tmp_path / "JARN.md").is_file()


def test_init_refuses_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "JARN.md").write_text("existing\n", encoding="utf-8")
    assert main(["init"]) == 1
    assert "init --force" in capsys.readouterr().err


def test_init_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "JARN.md").write_text("old\n", encoding="utf-8")
    assert main(["init", "--force"]) == 0
    assert "old" not in (tmp_path / "JARN.md").read_text(encoding="utf-8")
