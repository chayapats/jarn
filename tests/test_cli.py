"""CLI subcommand tests (non-interactive paths)."""

from __future__ import annotations

import json

import yaml

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


# ---------------------------------------------------------------------------
# FIX 6: doctor --json surfaces git.autocheckpoint, wiki.enabled,
#         observability.transcript, context.repo_map + token budget
# ---------------------------------------------------------------------------


def _make_doctor_config(tmp_path, *, autocheckpoint=False, wiki_enabled=True,
                        transcript=True, repo_map="tool", repo_map_tokens=512):
    """Write a minimal config YAML and return its path."""
    gp = tmp_path / "config.yaml"
    gp.write_text(
        yaml.safe_dump({
            "default_profile": "openrouter",
            "providers": {
                "openrouter": {
                    "type": "openrouter",
                    "api_key": "sk-test",
                    "base_url": "http://localhost:9999/v1",
                }
            },
            "routing": {"main": "openrouter/some-model"},
            "git": {"autocheckpoint": autocheckpoint},
            "wiki": {"enabled": wiki_enabled},
            "observability": {"transcript": transcript},
            "context": {"repo_map": repo_map, "repo_map_tokens": repo_map_tokens},
        }),
        encoding="utf-8",
    )
    return gp


def test_doctor_json_includes_git_autocheckpoint(tmp_path, monkeypatch, capsys):
    """jarn doctor --json must include git.autocheckpoint."""
    from jarn import cli
    from jarn.config import paths

    gp = _make_doctor_config(tmp_path, autocheckpoint=True)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "git" in data, "doctor --json must include 'git' key"
    assert data["git"]["autocheckpoint"] is True


def test_doctor_json_includes_wiki_enabled(tmp_path, monkeypatch, capsys):
    """jarn doctor --json must include wiki.enabled."""
    from jarn import cli
    from jarn.config import paths

    gp = _make_doctor_config(tmp_path, wiki_enabled=True)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "wiki" in data, "doctor --json must include 'wiki' key"
    assert data["wiki"]["enabled"] is True


def test_doctor_json_includes_observability_transcript(tmp_path, monkeypatch, capsys):
    """jarn doctor --json must include observability.transcript."""
    from jarn import cli
    from jarn.config import paths

    gp = _make_doctor_config(tmp_path, transcript=False)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "observability" in data, "doctor --json must include 'observability' key"
    assert data["observability"]["transcript"] is False


def test_doctor_json_includes_context_repo_map(tmp_path, monkeypatch, capsys):
    """jarn doctor --json must include context.repo_map and context.repo_map_tokens."""
    from jarn import cli
    from jarn.config import paths

    gp = _make_doctor_config(tmp_path, repo_map="auto", repo_map_tokens=2048)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "context" in data, "doctor --json must include 'context' key"
    assert data["context"]["repo_map"] == "auto"
    assert data["context"]["repo_map_tokens"] == 2048
