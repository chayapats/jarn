"""Tests for the wiki knowledge-base feature.

Covers:
- WikiStore read/write/append/search/index_text round-trips.
- Page-name sanitization (path traversal rejected).
- Project vs global tier logic; project-trust gate on injection.
- Permission-bridge mapping for wiki tools.
- Agent tool-wiring (present when enabled, absent when disabled).
- Config validation for the wiki section.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

from jarn.memory.wiki import WikiStore, _sanitize_page_name  # noqa: PLC2701

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path, *, has_project: bool = True) -> WikiStore:
    """Return a WikiStore backed by temp directories."""
    global_dir = tmp_path / "global" / "wiki"
    project_dir = (tmp_path / "project" / "wiki") if has_project else None
    return WikiStore(global_wiki_dir=global_dir, project_wiki_dir=project_dir)


# ---------------------------------------------------------------------------
# Page-name sanitization
# ---------------------------------------------------------------------------


def test_sanitize_rejects_dotdot() -> None:
    with pytest.raises(ValueError, match="\\.\\."):
        _sanitize_page_name("../../etc/passwd")


def test_sanitize_rejects_dotdot_in_middle() -> None:
    with pytest.raises(ValueError, match="\\.\\."):
        _sanitize_page_name("a/../b")


def test_sanitize_rejects_slash() -> None:
    with pytest.raises(ValueError, match="path separators"):
        _sanitize_page_name("a/b")


def test_sanitize_rejects_backslash() -> None:
    with pytest.raises(ValueError, match="path separators"):
        _sanitize_page_name("a\\b")


def test_sanitize_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        _sanitize_page_name("")


def test_sanitize_normal_name() -> None:
    slug = _sanitize_page_name("My Page Name")
    assert slug == "my-page-name"


def test_sanitize_special_chars_stripped() -> None:
    slug = _sanitize_page_name("hello<world>!")
    # Trailing separators are stripped by the slug function.
    assert "hello" in slug and "world" in slug


def test_sanitize_truncates_long_names() -> None:
    long_name = "a" * 200
    slug = _sanitize_page_name(long_name)
    assert len(slug) <= 80


# ---------------------------------------------------------------------------
# Write + read round-trip
# ---------------------------------------------------------------------------


def test_write_then_read(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ref = store.write("my-page", "# Hello\n\nContent here.", tier="project")
    assert "my-page" in ref
    content = store.read("my-page")
    assert "Content here." in content


def test_write_creates_file_inside_wiki_dir(tmp_path: Path) -> None:
    """The written file must be inside the wiki dir — never outside it."""
    store = _make_store(tmp_path)
    store.write("safe-page", "body", tier="project")
    project_pages = tmp_path / "project" / "wiki" / "pages"
    assert (project_pages / "safe-page.md").is_file()
    # Assert nothing was written outside the wiki dir.
    outside = tmp_path / "safe-page.md"
    assert not outside.exists()


def test_write_traversal_attempt_stays_inside(tmp_path: Path) -> None:
    """A page name that looks like a traversal is sanitized, not followed."""
    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.write("../../etc/passwd", "evil", tier="project")


def test_write_slash_attempt_rejected(tmp_path: Path) -> None:
    """A page name with a path separator is rejected outright."""
    store = _make_store(tmp_path)
    with pytest.raises(ValueError, match="path separators"):
        store.write("a/b", "body", tier="project")


def test_read_missing_page_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read("nonexistent-page")


def test_write_overwrites_existing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.write("page1", "version 1", tier="global")
    store.write("page1", "version 2", tier="global")
    assert "version 2" in store.read("page1")
    assert "version 1" not in store.read("page1")


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def test_append_adds_to_existing_page(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.write("notes", "# Notes\n\nFirst.", tier="project")
    store.append("notes", "Second note.")
    content = store.read("notes")
    assert "First." in content
    assert "Second note." in content


def test_append_creates_page_when_absent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.append("new-page", "Created by append.", tier="global")
    content = store.read("new-page")
    assert "Created by append." in content


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_finds_matching_lines(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.write("architecture", "# Arch\n\nUses postgres as the main DB.", tier="project")
    store.write("other", "# Other\n\nnothing relevant here.", tier="project")
    results = store.search("postgres")
    slugs = [slug for slug, _ in results]
    assert "architecture" in slugs
    assert "other" not in slugs


def test_search_case_insensitive(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.write("notes", "Uses PostgreSQL.\n", tier="global")
    results = store.search("postgresql")
    assert results
    results2 = store.search("POSTGRESQL")
    assert results2


def test_search_empty_when_no_match(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.write("page", "nothing here", tier="global")
    assert store.search("xyzzy_not_found") == []


def test_search_returns_matching_lines(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.write(
        "guide",
        "# Guide\n\nStep 1: install python.\nStep 2: run tests.\nEnd.",
        tier="global",
    )
    results = store.search("step")
    assert results
    slug, lines = results[0]
    assert slug == "guide"
    assert any("Step 1" in ln for ln in lines)
    assert any("Step 2" in ln for ln in lines)
    # "End." should not be in the matched lines.
    assert not any("End." in ln for ln in lines)


# ---------------------------------------------------------------------------
# Index text
# ---------------------------------------------------------------------------


def test_index_text_empty_when_no_pages(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.index_text() == ""


def test_index_text_lists_pages(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.write("api-design", "# API Design\n\nRESTful.", tier="project")
    store.write("deployment", "# Deployment\n\nUses Docker.", tier="global")
    idx = store.index_text()
    assert "api-design" in idx
    assert "deployment" in idx


def test_index_text_includes_summary(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.write("decisions", "# Decisions\n\nWe chose Postgres for durability.", tier="global")
    idx = store.index_text()
    assert "Postgres" in idx or "chose" in idx


def test_index_text_project_first(tmp_path: Path) -> None:
    """Project pages appear before global pages in the index."""
    store = _make_store(tmp_path)
    store.write("global-page", "global content", tier="global")
    store.write("project-page", "project content", tier="project")
    idx = store.index_text()
    # project-page should come before global-page in the output.
    assert idx.index("project-page") < idx.index("global-page")


# ---------------------------------------------------------------------------
# Project tier vs global tier
# ---------------------------------------------------------------------------


def test_project_page_shadows_global(tmp_path: Path) -> None:
    """A project page with the same slug shadows the global one on read."""
    store = _make_store(tmp_path)
    store.write("shared", "global version", tier="global")
    store.write("shared", "project version", tier="project")
    content = store.read("shared")
    assert "project version" in content
    assert "global version" not in content


def test_global_only_store_no_project(tmp_path: Path) -> None:
    """WikiStore without a project dir still works for global pages."""
    store = _make_store(tmp_path, has_project=False)
    store.write("global-note", "Global only.", tier="global")
    assert "Global only." in store.read("global-note")


def test_write_falls_back_to_global_when_no_project(tmp_path: Path) -> None:
    """Writing to 'project' tier with no project_wiki_dir falls back to global."""
    store = _make_store(tmp_path, has_project=False)
    store.write("fallback", "Fell back to global.", tier="project")
    # The file should be under the global pages dir.
    global_page = tmp_path / "global" / "wiki" / "pages" / "fallback.md"
    assert global_page.is_file()


# ---------------------------------------------------------------------------
# Project-trust gate on index injection in build_runtime
# ---------------------------------------------------------------------------


def _make_wiki_config(*, enabled: bool, has_project: bool = True):
    from jarn.config.schema import (
        Config,
        ProviderConfig,
        ProviderType,
        RoutingConfig,
        WikiConfig,
    )

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
        wiki=WikiConfig(enabled=enabled),
    )
    return cfg


def test_wiki_index_not_injected_when_project_untrusted(tmp_path: Path) -> None:
    """With project_trusted=False, project wiki pages must not appear in the
    system prompt, though global wiki is still injected."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime
    from jarn.memory.wiki import WikiStore

    cfg = _make_wiki_config(enabled=True)
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    # Write a project page and a global page.
    store = WikiStore.build(root)
    store.write("secret-project-page", "Sensitive project content.", tier="project")
    store.write("global-note", "Public global content.", tier="global")

    captured: dict[str, str] = {}

    def _fake_create_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured["system_prompt"] = kwargs.get("system_prompt", "")
        import deepagents

        return deepagents.create_deep_agent(**kwargs)

    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        patch("deepagents.create_deep_agent", side_effect=_fake_create_agent),
        contextlib.suppress(Exception),
    ):
        build_runtime(cfg, project_root=root, project_trusted=False)

    sp = captured.get("system_prompt", "")
    # Project page must NOT be in the system prompt.
    assert "secret-project-page" not in sp
    # Global page MUST be in the system prompt.
    assert "global-note" in sp


def test_wiki_index_injected_when_project_trusted(tmp_path: Path) -> None:
    """With project_trusted=True, both project and global pages appear."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime
    from jarn.memory.wiki import WikiStore

    cfg = _make_wiki_config(enabled=True)
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    store = WikiStore.build(root)
    store.write("project-page", "Project content.", tier="project")
    store.write("global-note", "Global content.", tier="global")

    captured: dict[str, str] = {}

    def _fake_create_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured["system_prompt"] = kwargs.get("system_prompt", "")
        import deepagents

        return deepagents.create_deep_agent(**kwargs)

    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        patch("deepagents.create_deep_agent", side_effect=_fake_create_agent),
        contextlib.suppress(Exception),
    ):
        build_runtime(cfg, project_root=root, project_trusted=True)

    sp = captured.get("system_prompt", "")
    assert "project-page" in sp
    assert "global-note" in sp


# ---------------------------------------------------------------------------
# Permission gating — bridge-level assertions
# ---------------------------------------------------------------------------


def test_wiki_write_maps_to_write_action() -> None:
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.permissions import ActionKind

    action = tool_to_action("wiki_write", {"page": "my-page", "content": "body"})
    assert action.kind is ActionKind.WRITE
    assert action.target == "my-page"
    assert action.tool == "wiki_write"


def test_wiki_append_maps_to_write_action() -> None:
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.permissions import ActionKind

    action = tool_to_action("wiki_append", {"page": "notes", "text": "more"})
    assert action.kind is ActionKind.WRITE
    assert action.target == "notes"
    assert action.tool == "wiki_append"


def test_wiki_search_maps_to_read_action() -> None:
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.permissions import ActionKind

    action = tool_to_action("wiki_search", {"query": "postgres"})
    assert action.kind is ActionKind.READ


def test_wiki_read_maps_to_read_action() -> None:
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.permissions import ActionKind

    action = tool_to_action("wiki_read", {"page": "my-page"})
    assert action.kind is ActionKind.READ


def test_wiki_mutating_tools_in_interrupt_map() -> None:
    """wiki_write and wiki_append must be gatable via interrupt_map extra_tools."""
    from jarn.agent.permissions_bridge import WIKI_MUTATING_TOOLS, interrupt_map

    m = interrupt_map(list(WIKI_MUTATING_TOOLS))
    assert "wiki_write" in m
    assert "wiki_append" in m


def test_wiki_readonly_tools_not_in_base_interrupt_map() -> None:
    """wiki_search and wiki_read are never in the base interrupt_map."""
    from jarn.agent.permissions_bridge import interrupt_map

    m = interrupt_map()
    assert "wiki_search" not in m
    assert "wiki_read" not in m


def test_wiki_write_auto_allowed_in_auto_edit(tmp_path: Path) -> None:
    """In auto-edit mode, wiki_write (WRITE action) is auto-allowed when the target
    path resolves inside the project root."""
    from jarn.config.schema import PermissionMode
    from jarn.permissions import PermissionEngine

    # The wiki page path target must be a full path inside the project to be
    # considered in-scope by the engine.  Construct one that is inside tmp_path.
    wiki_target = str(tmp_path / ".jarn" / "wiki" / "pages" / "my-page.md")
    engine = PermissionEngine(mode=PermissionMode.AUTO_EDIT, project_root=tmp_path)
    # Directly construct the action with the full path (simulating a richer bridge).
    from jarn.permissions import Action, ActionKind

    action = Action(ActionKind.WRITE, target=wiki_target, tool="wiki_write")
    result = engine.evaluate(action)
    assert result.decision.value == "allow"


def test_wiki_write_slug_asks_in_auto_edit_without_full_path(tmp_path: Path) -> None:
    """When the target is a bare slug (not a full path), the engine cannot confirm
    it's in-scope, so auto-edit falls back to ASK — consistent with the engine's
    general behaviour for WRITE actions with non-resolvable targets."""
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.config.schema import PermissionMode
    from jarn.permissions import PermissionEngine

    engine = PermissionEngine(mode=PermissionMode.AUTO_EDIT, project_root=tmp_path)
    action = tool_to_action("wiki_write", {"page": "my-page"})
    result = engine.evaluate(action)
    # A bare slug is out-of-scope from the engine's perspective; it prompts.
    assert result.decision.value == "ask"


def test_wiki_write_prompts_in_ask_mode(tmp_path: Path) -> None:
    """In ask mode, wiki_write (WRITE action) triggers a prompt."""
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.config.schema import PermissionMode
    from jarn.permissions import PermissionEngine

    engine = PermissionEngine(mode=PermissionMode.ASK, project_root=tmp_path)
    action = tool_to_action("wiki_write", {"page": "my-page"})
    result = engine.evaluate(action)
    assert result.decision.value == "ask"


# ---------------------------------------------------------------------------
# Tool wiring in build_runtime
# ---------------------------------------------------------------------------


def _build_cfg(*, wiki_enabled: bool):
    from jarn.config.schema import (
        Config,
        ContextConfig,
        ProviderConfig,
        ProviderType,
        RoutingConfig,
        WikiConfig,
    )

    return Config(
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
        wiki=WikiConfig(enabled=wiki_enabled),
    )


def test_wiki_tools_present_when_enabled(tmp_path: Path) -> None:
    """With wiki.enabled=True, the agent tool list contains all four wiki tools."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime

    cfg = _build_cfg(wiki_enabled=True)
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    captured_tools: list = []

    def _fake_create_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured_tools.extend(kwargs.get("tools") or [])
        import deepagents

        return deepagents.create_deep_agent(**kwargs)

    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        patch("deepagents.create_deep_agent", side_effect=_fake_create_agent),
        contextlib.suppress(Exception),
    ):
        build_runtime(cfg, project_root=root)

    tool_names = {getattr(t, "name", "") for t in captured_tools}
    assert "wiki_search" in tool_names
    assert "wiki_read" in tool_names
    assert "wiki_write" in tool_names
    assert "wiki_append" in tool_names


def test_wiki_tools_absent_when_disabled(tmp_path: Path) -> None:
    """With wiki.enabled=False, no wiki tools are registered."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime

    cfg = _build_cfg(wiki_enabled=False)
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    captured_tools: list = []

    def _fake_create_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured_tools.extend(kwargs.get("tools") or [])
        import deepagents

        return deepagents.create_deep_agent(**kwargs)

    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        patch("deepagents.create_deep_agent", side_effect=_fake_create_agent),
        contextlib.suppress(Exception),
    ):
        build_runtime(cfg, project_root=root)

    tool_names = {getattr(t, "name", "") for t in captured_tools}
    assert "wiki_search" not in tool_names
    assert "wiki_read" not in tool_names
    assert "wiki_write" not in tool_names
    assert "wiki_append" not in tool_names


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_wiki_enabled_true() -> None:
    from jarn.config.loader import _build_config

    cfg = _build_config({"wiki": {"enabled": True}})
    assert cfg.wiki.enabled is True


def test_config_wiki_enabled_false() -> None:
    from jarn.config.loader import _build_config

    cfg = _build_config({"wiki": {"enabled": False}})
    assert cfg.wiki.enabled is False


def test_config_wiki_default_disabled() -> None:
    from jarn.config.loader import _build_config

    cfg = _build_config({})
    assert cfg.wiki.enabled is False


def test_config_wiki_bad_value_raises() -> None:
    from jarn.config.loader import ConfigError, _build_config

    with pytest.raises(ConfigError, match="wiki.enabled"):
        _build_config({"wiki": {"enabled": "maybe"}})


# ---------------------------------------------------------------------------
# WikiStore.build() — path helpers integration
# ---------------------------------------------------------------------------


def test_wiki_store_build_global_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WikiStore.build() uses paths.global_wiki_dir() for the global tier."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    store = WikiStore.build(project_root=None)
    from jarn.config import paths

    assert store.global_wiki_dir == paths.global_wiki_dir()


def test_wiki_store_build_project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WikiStore.build() resolves the project wiki dir from the project root."""
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    store = WikiStore.build(project_root=root)
    from jarn.config import paths

    assert store.project_wiki_dir == paths.project_wiki_dir(root)


# ---------------------------------------------------------------------------
# FIX 4: wiki tools must not surface project pages when project is untrusted
# ---------------------------------------------------------------------------


def _build_wiki_cfg(*, enabled: bool = True):  # type: ignore[return]
    from jarn.config.schema import (
        Config,
        ContextConfig,
        ProviderConfig,
        ProviderType,
        RoutingConfig,
        WikiConfig,
    )

    return Config(
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
        wiki=WikiConfig(enabled=enabled),
    )


def test_wiki_tool_does_not_surface_project_pages_when_untrusted(tmp_path: Path) -> None:
    """With project_trusted=False, wiki tools must not expose project-tier wiki pages.

    This test fails without FIX 4: before the fix, _add_wiki_tools builds the
    tool-facing store with WikiStore.build(root), which includes the project
    tier.  An untrusted project could therefore serve hostile content via
    wiki_read/wiki_search — a prompt-injection vector.
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime

    cfg = _build_wiki_cfg(enabled=True)
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    # Write a project wiki page with a distinctive marker.
    store = WikiStore.build(root)
    store.write("secret-project-wiki", "UNTRUSTED_PROJECT_WIKI_CONTENT", tier="project")
    store.write("safe-global-wiki", "GLOBAL_WIKI_CONTENT", tier="global")

    # We'll capture the tools registered with the agent so we can exercise them.
    captured_tools: list = []

    def _fake_create_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured_tools.extend(kwargs.get("tools") or [])
        import deepagents
        return deepagents.create_deep_agent(**kwargs)

    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        patch("deepagents.create_deep_agent", side_effect=_fake_create_agent),
        contextlib.suppress(Exception),
    ):
        build_runtime(cfg, project_root=root, project_trusted=False)

    # Find the wiki_search tool among the registered tools.
    wiki_search_tool = next(
        (t for t in captured_tools if getattr(t, "name", "") == "wiki_search"),
        None,
    )
    assert wiki_search_tool is not None, "wiki_search tool must be registered"

    # Invoke wiki_search — project pages must NOT be matched (i.e. the response
    # must be the "no pages matched" message, not actual content lines).
    # The query string appears in the "no pages matched" error message itself,
    # so we check that no actual matched lines are returned (no "secret-project-wiki"
    # slug header appears).
    search_result = wiki_search_tool.func("UNTRUSTED_PROJECT_WIKI_CONTENT")
    assert "secret-project-wiki" not in search_result, (
        "wiki_search must not surface project wiki pages when project is untrusted"
    )

    # Global pages should still be accessible.
    global_result = wiki_search_tool.func("GLOBAL_WIKI_CONTENT")
    assert "GLOBAL_WIKI_CONTENT" in global_result, (
        "wiki_search must still surface global wiki pages even when project is untrusted"
    )


def test_wiki_tool_surfaces_project_pages_when_trusted(tmp_path: Path) -> None:
    """With project_trusted=True, wiki tools can read project-tier pages."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime

    cfg = _build_wiki_cfg(enabled=True)
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    store = WikiStore.build(root)
    store.write("trusted-project-wiki", "TRUSTED_PROJECT_WIKI_CONTENT", tier="project")

    captured_tools: list = []

    def _fake_create_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured_tools.extend(kwargs.get("tools") or [])
        import deepagents
        return deepagents.create_deep_agent(**kwargs)

    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        patch("deepagents.create_deep_agent", side_effect=_fake_create_agent),
        contextlib.suppress(Exception),
    ):
        build_runtime(cfg, project_root=root, project_trusted=True)

    wiki_search_tool = next(
        (t for t in captured_tools if getattr(t, "name", "") == "wiki_search"),
        None,
    )
    assert wiki_search_tool is not None

    result = wiki_search_tool.func("TRUSTED_PROJECT_WIKI_CONTENT")
    assert "TRUSTED_PROJECT_WIKI_CONTENT" in result, (
        "wiki_search must surface project pages when project is trusted"
    )
