"""Tests for AGENTS.md / CLAUDE.md + .claude/ interop (CompatConfig feature)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from jarn.config.loader import load_config
from jarn.extensibility.commands import load_commands
from jarn.extensibility.skills import load_skills
from jarn.memory.context import assemble_system_context, project_context_text, resolve_context_file

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

    _write_command(root / ".claude" / "commands", "summarize", "claude summarize")
    _write_command(root / ".jarn" / "commands", "summarize", "jarn summarize")

    cmds = load_commands(root, read_claude_dir=True)
    assert cmds["summarize"].description == "jarn summarize"


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


# ── FIX 1: build_runtime must forward compat settings ────────────────────────


def test_build_runtime_forwards_read_claude_dir_false(
    tmp_path: Path, monkeypatch: object
) -> None:
    """build_runtime must forward compat.read_claude_dir=False to load_skills/load_commands.

    This test fails without FIX 1: without the fix, build_runtime calls
    load_skills / load_commands without read_claude_dir, so the .claude/skills
    dir is always consulted regardless of the compat setting.
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime
    from jarn.config.schema import (
        CompatConfig,
        Config,
        ContextConfig,
        ProviderConfig,
        ProviderType,
        RoutingConfig,
        WikiConfig,
    )
    from jarn.providers.models import ModelFactory

    # Config with read_claude_dir=False and a custom context file list.
    cfg = Config(
        default_profile="openrouter",
        providers={
            "openrouter": ProviderConfig(
                type=ProviderType.OPENROUTER,
                api_key="sk-test",
                base_url="http://localhost:9999/v1",
            )
        },
        routing=RoutingConfig(main="openrouter/anthropic/claude-opus-4-8"),
        context=ContextConfig(repo_map="off"),
        wiki=WikiConfig(enabled=False),
        compat=CompatConfig(
            read_claude_dir=False,
            context_files=["AGENTS.md"],
        ),
    )

    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    # Write a skill under .claude/skills — it should NOT be loaded with read_claude_dir=False.
    claude_skill_dir = root / ".claude" / "skills"
    claude_skill_dir.mkdir(parents=True)
    (claude_skill_dir / "injected.md").write_text(
        "---\nname: injected\ndescription: hostile\ntrigger: auto\n---\nBad skill.",
        encoding="utf-8",
    )
    # Write AGENTS.md but NOT JARN.md — context_files=["AGENTS.md"] should pick it up.
    (root / "AGENTS.md").write_text("AGENTS_CONTEXT_MARKER", encoding="utf-8")
    (root / "JARN.md").write_text("JARN_MARKER_SHOULD_NOT_APPEAR", encoding="utf-8")

    # Use monkeypatching to capture the kwargs forwarded to load_skills/load_commands
    # so we can assert the correct values were passed without running the full agent.
    captured: dict[str, object] = {}

    import jarn.extensibility.commands as _cmds_mod
    import jarn.extensibility.skills as _skills_mod

    original_load_skills = _skills_mod.load_skills
    original_load_commands = _cmds_mod.load_commands

    def _spy_skills(*args: object, **kwargs: object) -> object:
        captured["skills_kwargs"] = kwargs
        return original_load_skills(*args, **kwargs)

    def _spy_commands(*args: object, **kwargs: object) -> object:
        captured["commands_kwargs"] = kwargs
        return original_load_commands(*args, **kwargs)

    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch.object(ModelFactory, "build", return_value=fake),
        patch("jarn.agent.builder.load_skills", side_effect=_spy_skills),
        patch("jarn.agent.builder.load_commands", side_effect=_spy_commands),
        patch("deepagents.create_deep_agent", return_value=object()),
    ):
        build_runtime(cfg, project_root=root, project_trusted=True)

    # FIX 1 assertion: read_claude_dir must have been forwarded as False.
    assert captured.get("skills_kwargs", {}).get("read_claude_dir") is False, (
        "build_runtime did not forward compat.read_claude_dir=False to load_skills"
    )
    assert captured.get("commands_kwargs", {}).get("read_claude_dir") is False, (
        "build_runtime did not forward compat.read_claude_dir=False to load_commands"
    )


def test_build_runtime_forwards_context_files(
    tmp_path: Path,
) -> None:
    """build_runtime must forward compat.context_files to assemble_system_context.

    This test fails without FIX 1: without the fix, assemble_system_context is
    called without context_files, so it always uses the default list rather
    than the configured one.
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime
    from jarn.config.schema import (
        CompatConfig,
        Config,
        ContextConfig,
        ProviderConfig,
        ProviderType,
        RoutingConfig,
        WikiConfig,
    )
    from jarn.providers.models import ModelFactory

    cfg = Config(
        default_profile="openrouter",
        providers={
            "openrouter": ProviderConfig(
                type=ProviderType.OPENROUTER,
                api_key="sk-test",
                base_url="http://localhost:9999/v1",
            )
        },
        routing=RoutingConfig(main="openrouter/anthropic/claude-opus-4-8"),
        context=ContextConfig(repo_map="off"),
        wiki=WikiConfig(enabled=False),
        compat=CompatConfig(
            read_claude_dir=True,
            context_files=["AGENTS.md"],  # only AGENTS.md, not JARN.md
        ),
    )

    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    # Only write AGENTS.md — with context_files=["AGENTS.md"] it should be chosen.
    (root / "AGENTS.md").write_text("AGENTS_CONTEXT_MARKER", encoding="utf-8")
    # JARN.md also present but should NOT be chosen because it's not in context_files.
    (root / "JARN.md").write_text("JARN_MARKER_MUST_NOT_APPEAR", encoding="utf-8")

    captured_context: dict[str, object] = {}

    import jarn.memory.context as _ctx_mod

    original_assemble = _ctx_mod.assemble_system_context

    def _spy_assemble(*args: object, **kwargs: object) -> str:
        captured_context["kwargs"] = kwargs
        return original_assemble(*args, **kwargs)

    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch.object(ModelFactory, "build", return_value=fake),
        patch("jarn.agent.builder.assemble_system_context", side_effect=_spy_assemble),
        patch("deepagents.create_deep_agent", return_value=object()),
    ):
        build_runtime(cfg, project_root=root, project_trusted=True)

    # FIX 1 assertion: context_files must have been forwarded.
    assert captured_context.get("kwargs", {}).get("context_files") == ["AGENTS.md"], (
        "build_runtime did not forward compat.context_files to assemble_system_context"
    )


# ── resolve_context_file + startup echo (P5.D) ───────────────────────────────


def test_resolve_context_file_jarn_md(tmp_path: Path) -> None:
    """resolve_context_file returns JARN.md when it exists (highest priority)."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "JARN.md").write_text("# J", encoding="utf-8")
    (root / "AGENTS.md").write_text("# A", encoding="utf-8")
    result = resolve_context_file(root)
    assert result is not None
    assert result.name == "JARN.md"


def test_resolve_context_file_agents_md_fallback(tmp_path: Path) -> None:
    """resolve_context_file falls back to AGENTS.md when JARN.md is absent."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "AGENTS.md").write_text("# A", encoding="utf-8")
    result = resolve_context_file(root)
    assert result is not None
    assert result.name == "AGENTS.md"


def test_resolve_context_file_claude_md_fallback(tmp_path: Path) -> None:
    """resolve_context_file falls back to CLAUDE.md as last resort."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# C", encoding="utf-8")
    result = resolve_context_file(root)
    assert result is not None
    assert result.name == "CLAUDE.md"


def test_resolve_context_file_none_when_absent(tmp_path: Path) -> None:
    """resolve_context_file returns None when none of the candidates exist."""
    root = tmp_path / "proj"
    root.mkdir()
    assert resolve_context_file(root) is None


def test_resolve_context_file_custom_order(tmp_path: Path) -> None:
    """A custom context_files order is respected by resolve_context_file."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# C", encoding="utf-8")
    (root / "AGENTS.md").write_text("# A", encoding="utf-8")
    result = resolve_context_file(root, context_files=["CLAUDE.md", "AGENTS.md"])
    assert result is not None
    assert result.name == "CLAUDE.md"


def _render_startup_echo(root: Path, context_files: list[str] | None = None) -> str:
    """Reproduce the startup-echo logic from repl.py run() in isolation."""
    out = StringIO()
    console = Console(file=out, highlight=False, markup=False, width=120)
    from jarn.tui import palette
    ctx_path = resolve_context_file(root, context_files=context_files)
    if ctx_path is not None:
        console.print(
            f"[{palette.C_DIM}]context: {ctx_path.name}[/{palette.C_DIM}]"
        )
    return out.getvalue()


def test_startup_echo_names_loaded_context_file(tmp_path: Path) -> None:
    """Startup echo prints the name of the context file that was loaded."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "JARN.md").write_text("# JARN context", encoding="utf-8")
    output = _render_startup_echo(root)
    assert "JARN.md" in output


def test_startup_echo_names_agents_md_when_jarn_absent(tmp_path: Path) -> None:
    """Startup echo names AGENTS.md when JARN.md is not present."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "AGENTS.md").write_text("# Agents context", encoding="utf-8")
    output = _render_startup_echo(root)
    assert "AGENTS.md" in output


def test_startup_echo_names_claude_md_as_last_resort(tmp_path: Path) -> None:
    """Startup echo names CLAUDE.md when it is the only context file present."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# Claude context", encoding="utf-8")
    output = _render_startup_echo(root)
    assert "CLAUDE.md" in output


def test_startup_echo_silent_when_no_context_file(tmp_path: Path) -> None:
    """Startup echo is silent when no context file exists."""
    root = tmp_path / "proj"
    root.mkdir()
    output = _render_startup_echo(root)
    assert "context:" not in output
