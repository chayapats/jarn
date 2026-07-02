"""``jarn doctor`` extension diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from jarn import cli
from jarn.config.loader import load_config
from jarn.doctor_extensions import collect_extensions


def _write_global(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "providers:\n  openrouter:\n    type: openrouter\n    api_key: sk-test\n"
        "    base_url: https://openrouter.ai/api/v1\n",
        encoding="utf-8",
    )


def test_collect_extensions_shadowing_and_builtin_rename(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("JARN_HOME", str(home))
    _write_global(home)

    (home / "skills").mkdir()
    (home / "skills" / "shared.md").write_text(
        "---\nname: shared\ndescription: global\n---\nbody", encoding="utf-8"
    )
    (home / "commands").mkdir()
    (home / "commands" / "cost.md").write_text("---\n---\nshadow builtin", encoding="utf-8")

    root = tmp_path / "proj"
    (root / ".jarn" / "skills").mkdir(parents=True)
    (root / ".jarn" / "skills" / "shared.md").write_text(
        "---\nname: shared\ndescription: project\n---\nbody", encoding="utf-8"
    )
    (root / ".jarn" / "skills" / "local.md").write_text(
        "---\nname: local\ndescription: only here\n---\nbody", encoding="utf-8"
    )

    cfg = load_config(project_root=root, project_trusted=True)
    ext = collect_extensions(root, project_trusted=True, config=cfg)

    assert ext["counts"]["skills"] == 2
    skills = {r["name"]: r for r in ext["skills"]}
    assert skills["shared"]["status"] == "active"
    assert skills["shared"]["scope"] == "project"
    assert skills["local"]["status"] == "active"
    shadowed = [r for r in ext["skills"] if r["status"] == "shadowed"]
    assert len(shadowed) == 1
    assert shadowed[0]["scope"] == "global"

    renamed = [r for r in ext["commands"] if r["status"] == "renamed_builtin"]
    assert len(renamed) == 1
    assert renamed[0]["name"] == "cost"
    assert "cost-custom" in renamed[0]["detail"]


def test_collect_extensions_skips_untrusted_project_tier(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("JARN_HOME", str(home))
    _write_global(home)

    root = tmp_path / "proj"
    (root / ".jarn" / "skills").mkdir(parents=True)
    (root / ".jarn" / "skills" / "secret.md").write_text(
        "---\nname: secret\ndescription: x\n---\nbody", encoding="utf-8"
    )

    cfg = load_config(project_root=root, project_trusted=False)
    ext = collect_extensions(root, project_trusted=False, config=cfg)

    assert ext["counts"]["skills"] == 0
    skipped = [r for r in ext["skills"] if r["status"] == "skipped_untrusted"]
    assert len(skipped) == 1
    assert ext["warnings"]


def test_doctor_json_includes_extensions(isolated_home, tmp_path, monkeypatch, capsys):
    from unittest.mock import patch

    from jarn.config import paths

    gp = isolated_home / "config.yaml"
    gp.write_text(
        "providers:\n  openrouter:\n    type: openrouter\n    api_key: sk-test\n"
        "    base_url: https://openrouter.ai/api/v1\n",
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    (proj / ".jarn" / "skills").mkdir(parents=True)
    (proj / ".jarn" / "skills" / "lint.md").write_text(
        "---\nname: lint\ndescription: lint\n---\nrun lint", encoding="utf-8"
    )

    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: proj)

    with patch("jarn.providers.ModelFactory.build_main", return_value=object()):
        cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "extensions" in data
    assert data["extensions"]["counts"]["skills"] >= 1


def test_cli_and_command_parity(tmp_path, monkeypatch, base_config):
    """``jarn doctor`` and ``/doctor`` render identical Rich markup."""
    from io import StringIO
    from unittest.mock import patch

    from rich.console import Console

    from jarn.config import paths
    from jarn.doctor.collect import collect_doctor
    from jarn.doctor.render import doctor_lines
    from jarn.tui.controller import Controller

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    gp = home / "config.yaml"
    gp.write_text(
        "providers:\n  openrouter:\n    type: openrouter\n    api_key: sk-test\n"
        "    base_url: http://localhost:9999/v1\n"
        "routing:\n  main: openrouter/anthropic/claude-opus-4-8\n",
        encoding="utf-8",
    )
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: root)

    with (
        patch("jarn.config.loader.load_config", return_value=base_config),
        patch("jarn.providers.ModelFactory.build_main", return_value=object()),
    ):
        diag_cli: dict = {}
        collect_doctor(diag_cli)
        cli_lines = doctor_lines(diag_cli)

        diag_cmd: dict = {}
        ctrl = Controller(base_config, root)
        collect_doctor(
            diag_cmd,
            config=ctrl.config,
            project_root=ctrl.project_root,
            project_trusted=ctrl.project_trusted,
        )
        cmd_lines = doctor_lines(diag_cmd)
        result = ctrl.handle_command("doctor", "")
        ctrl.close()

    assert cli_lines == cmd_lines
    assert result.text == "\n".join(cli_lines)

    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    console.print(result.text)
    rendered = buf.getvalue()
    assert "Providers" in rendered
    assert "Extensions" in rendered


def test_skill_shadow_matches_runtime(monkeypatch, tmp_path):
    """Doctor and runtime agree when .claude and .jarn define the same skill."""
    from jarn.config.loader import load_config
    from jarn.extensibility.skills import load_skills

    home = tmp_path / "home"
    monkeypatch.setenv("JARN_HOME", str(home))
    _write_global(home)

    root = tmp_path / "proj"
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".claude" / "skills" / "shared.md").write_text(
        "---\nname: shared\ndescription: claude\n---\nclaude body",
        encoding="utf-8",
    )
    (root / ".jarn" / "skills").mkdir(parents=True)
    (root / ".jarn" / "skills" / "shared.md").write_text(
        "---\nname: shared\ndescription: jarn\n---\njarn body",
        encoding="utf-8",
    )

    runtime = load_skills(root, project_trusted=True)
    assert runtime["shared"].description == "jarn"
    assert runtime["shared"].scope == "project"

    cfg = load_config(project_root=root, project_trusted=True)
    ext = collect_extensions(root, project_trusted=True, config=cfg)
    skills = {r["name"]: r for r in ext["skills"]}
    assert skills["shared"]["status"] == "active"
    assert skills["shared"]["scope"] == "project"
    shadowed = [r for r in ext["skills"] if r["status"] == "shadowed"]
    assert len(shadowed) == 1
    assert shadowed[0]["scope"] == "project"
    assert "claude" in shadowed[0]["path"]
