"""Tests for AGENTS.md / CLAUDE.md + .claude/ interop (CompatConfig feature)."""

from __future__ import annotations

from pathlib import Path

from jarn.config.loader import load_config
from jarn.extensibility.commands import load_commands
from jarn.extensibility.skills import load_skills
from jarn.memory.context import assemble_system_context, project_context_text

# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_skill(dirpath: Path, name: str, trigger: str = "auto") -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: does {name}\ntrigger: {trigger}\n---\n"
        f"Instructions for {name}.",
        encoding="utf-8",
    )


def _write_command(dirpath: Path, name: str, description: str = "") -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{name}.md").write_text(
        f"---\ndescription: {description}\n---\nDo {name}: $ARGS",
        encoding="utf-8",
    )


# ── project_context_text / ordered file list ─────────────────────────────────


def test_agents_md_loaded_when_jarn_md_absent(tmp_path: Path) -> None:
    """AGENTS.md is used as the project context when JARN.md is not present."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "AGENTS.md").write_text("# Agents context", encoding="utf-8")

    ctx = project_context_text(root)
    assert ctx is not None
    assert "Agents context" in ctx


def test_jarn_md_wins_over_agents_md(tmp_path: Path) -> None:
    """JARN.md has priority when both JARN.md and AGENTS.md are present."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "JARN.md").write_text("# JARN context", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Agents context", encoding="utf-8")

    ctx = project_context_text(root)
    assert ctx is not None
    assert "JARN context" in ctx
    assert "Agents context" not in ctx


def test_claude_md_is_last_fallback(tmp_path: Path) -> None:
    """CLAUDE.md is the last resort when neither JARN.md nor AGENTS.md exist."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# Claude context", encoding="utf-8")

    ctx = project_context_text(root)
    assert ctx is not None
    assert "Claude context" in ctx


def test_context_file_ordering_is_respected(tmp_path: Path) -> None:
    """A custom context_files order is honoured."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# Claude", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Agents", encoding="utf-8")

    # Ask for CLAUDE.md first — it should win even though AGENTS.md is also present.
    ctx = project_context_text(root, context_files=["CLAUDE.md", "AGENTS.md"])
    assert ctx is not None
    assert "Claude" in ctx


def test_no_context_file_returns_none(tmp_path: Path) -> None:
    """None is returned when none of the candidate files exist."""
    root = tmp_path / "proj"
    root.mkdir()
    assert project_context_text(root) is None


def test_assemble_skips_project_context_when_untrusted(tmp_path: Path, monkeypatch) -> None:
    """project_trusted=False prevents project context from entering the prompt."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "AGENTS.md").write_text("# Secret agents context", encoding="utf-8")
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    result = assemble_system_context(root, project_trusted=False)
    assert "Secret agents context" not in result


# ── .claude/skills discovery ──────────────────────────────────────────────────


def test_claude_project_skill_is_discovered(tmp_path: Path, monkeypatch) -> None:
    """A skill under <project>/.claude/skills/<name>.md is loaded."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    _write_skill(root / ".claude" / "skills", "claude-skill")

    skills = load_skills(root, read_claude_dir=True)
    assert "claude-skill" in skills


def test_jarn_skill_overrides_claude_skill(tmp_path: Path, monkeypatch) -> None:
    """.jarn skill of the same name wins over .claude skill."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    # Write .claude version first (lower priority)
    claude_skill_dir = root / ".claude" / "skills"
    claude_skill_dir.mkdir(parents=True)
    (claude_skill_dir / "shared.md").write_text(
        "---\nname: shared\ndescription: from claude\ntrigger: auto\n---\nclaude body",
        encoding="utf-8",
    )

    # Write .jarn version (higher priority)
    jarn_skill_dir = root / ".jarn" / "skills"
    jarn_skill_dir.mkdir(parents=True)
    (jarn_skill_dir / "shared.md").write_text(
        "---\nname: shared\ndescription: from jarn\ntrigger: auto\n---\njarn body",
        encoding="utf-8",
    )

    skills = load_skills(root, read_claude_dir=True)
    assert skills["shared"].description == "from jarn"
    assert skills["shared"].body == "jarn body"


def test_read_claude_dir_false_excludes_claude_skills(tmp_path: Path, monkeypatch) -> None:
    """read_claude_dir=False disables .claude/skills discovery."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    _write_skill(root / ".claude" / "skills", "claude-only")

    skills = load_skills(root, read_claude_dir=False)
    assert "claude-only" not in skills


def test_untrusted_project_skips_claude_project_skills(tmp_path: Path, monkeypatch) -> None:
    """project_trusted=False prevents project-tier .claude/skills from loading."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    _write_skill(root / ".claude" / "skills", "hostile-skill")

    skills = load_skills(root, project_trusted=False, read_claude_dir=True)
    assert "hostile-skill" not in skills


def test_global_claude_skill_loads_without_trust(tmp_path: Path, monkeypatch) -> None:
    """Global ~/.claude/skills is not project-tier and loads regardless of trust."""
    home = tmp_path / "home"
    monkeypatch.setenv("JARN_HOME", str(home))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    # Patch global_claude_home to point to our temp dir
    from jarn.config import paths as jarn_paths
    monkeypatch.setattr(jarn_paths, "global_claude_home", lambda: tmp_path / "claude-home")
    monkeypatch.setattr(
        jarn_paths,
        "global_claude_subdir",
        lambda name: tmp_path / "claude-home" / name,
    )

    _write_skill(tmp_path / "claude-home" / "skills", "global-claude-skill")

    skills = load_skills(root, project_trusted=False, read_claude_dir=True)
    assert "global-claude-skill" in skills


# ── .claude/commands discovery ────────────────────────────────────────────────


def test_claude_project_command_is_discovered(tmp_path: Path, monkeypatch) -> None:
    """A command under <project>/.claude/commands is loaded."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    _write_command(root / ".claude" / "commands", "deploy", "deploy the app")

    cmds = load_commands(root, read_claude_dir=True)
    assert "deploy" in cmds
    assert cmds["deploy"].render("prod") == "Do deploy: prod"


def test_jarn_command_overrides_claude_command(tmp_path: Path, monkeypatch) -> None:
    """.jarn command of the same name beats .claude command."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    _write_command(root / ".claude" / "commands", "review", "claude review")
    _write_command(root / ".jarn" / "commands", "review", "jarn review")

    cmds = load_commands(root, read_claude_dir=True)
    assert cmds["review"].description == "jarn review"


def test_builtin_name_still_shadow_protected(tmp_path: Path, monkeypatch) -> None:
    """A .claude/commands file named after a built-in gets the -custom suffix."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    _write_command(root / ".claude" / "commands", "cost", "sneaky cost cmd")

    cmds = load_commands(root, read_claude_dir=True)
    assert "cost" not in cmds
    assert "cost-custom" in cmds


def test_read_claude_dir_false_excludes_claude_commands(tmp_path: Path, monkeypatch) -> None:
    """read_claude_dir=False disables .claude/commands discovery."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    _write_command(root / ".claude" / "commands", "deploy")

    cmds = load_commands(root, read_claude_dir=False)
    assert "deploy" not in cmds


def test_untrusted_project_skips_claude_project_commands(tmp_path: Path, monkeypatch) -> None:
    """project_trusted=False prevents project-tier .claude/commands from loading."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    _write_command(root / ".claude" / "commands", "hostile-cmd")

    cmds = load_commands(root, project_trusted=False, read_claude_dir=True)
    assert "hostile-cmd" not in cmds


# ── CompatConfig parsing ──────────────────────────────────────────────────────


def test_compat_defaults_when_section_absent(tmp_path: Path) -> None:
    """Config without a compat section yields correct defaults."""
    cfg = load_config(
        global_path=tmp_path / "missing.yaml",
        project_path=tmp_path / "missing2.yaml",
    )
    assert cfg.compat.context_files == ["JARN.md", "AGENTS.md", "CLAUDE.md"]
    assert cfg.compat.read_claude_dir is True


def test_compat_context_files_parsed(tmp_path: Path) -> None:
    """compat.context_files is read from config."""
    import yaml

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump({"compat": {"context_files": ["MYFILE.md", "AGENTS.md"]}}),
        encoding="utf-8",
    )
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.compat.context_files == ["MYFILE.md", "AGENTS.md"]


def test_compat_read_claude_dir_false(tmp_path: Path) -> None:
    """compat.read_claude_dir: false is parsed correctly."""
    import yaml

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump({"compat": {"read_claude_dir": False}}),
        encoding="utf-8",
    )
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.compat.read_claude_dir is False
