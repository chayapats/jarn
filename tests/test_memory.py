"""Memory store, project context, and session index tests."""

from __future__ import annotations

import pytest

from jarn.memory.context import assemble_system_context, init_template, write_jarn_md
from jarn.memory.sessions import SessionIndex, default_db_path, new_thread_id
from jarn.memory.store import Memory, MemoryStore, slugify  # noqa: F401 (slugify used indirectly)

# ---------------------------------------------------------------------------
# /memory dump helpers
# ---------------------------------------------------------------------------


def _make_controller(tmp_path, monkeypatch, base_config, *, trusted: bool = True):
    """Return a Controller rooted at a fresh temp project."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    from jarn.tui.controller import Controller
    return Controller(base_config, root, project_trusted=trusted)


def test_slugify():
    assert slugify("Hello World!") == "hello-world"
    assert slugify("Use AI SDK v6") == "use-ai-sdk-v6"


def test_save_and_load_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    mem = Memory(name="Likes TOML", description="prefers toml", body="The user likes config.", type="user")
    path = store.save(mem)
    assert path.is_file()
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].name == "Likes TOML"
    assert loaded[0].type == "user"


def test_invalid_memory_type_rejected(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError):
        store.save(Memory(name="x", description="y", body="z", type="bogus"))


def test_index_appended(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save(Memory(name="One", description="first", body="b", type="project"))
    store.save(Memory(name="Two", description="second", body="b", type="project"))
    index = store.index_text()
    assert "One" in index and "Two" in index


def test_index_updates_on_overwrite(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save(Memory(name="One", description="first", body="b", type="project"))
    store.save(Memory(name="One", description="updated", body="b", type="project"))

    index = store.index_text()
    assert "updated" in index
    assert "first" not in index


def test_get_and_delete_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save(Memory(name="One Thing", description="first", body="b", type="project"))
    (store.root / ".vectors.json").write_text("{}", encoding="utf-8")

    assert store.get("one-thing").name == "One Thing"
    assert store.delete("One Thing") is True
    assert store.get("one-thing") is None
    assert "One Thing" not in store.index_text()
    assert not (store.root / ".vectors.json").exists()
    assert store.delete("one-thing") is False


def test_thread_ids_unique():
    assert new_thread_id() != new_thread_id()


def test_session_index_roundtrip(tmp_path):
    idx = SessionIndex(tmp_path / "state.sqlite")
    idx.touch("t1", "first task", when=100.0)
    idx.touch("t2", "second task", when=200.0)
    idx.touch("t1", "would-be rename", when=300.0)
    sessions = idx.list()
    assert sessions[0].thread_id == "t1"  # most recent
    assert sessions[0].title == "first task"  # title sticks from first prompt
    assert sessions[0].updated_at == 300.0
    assert len(sessions) == 2


def test_write_jarn_md_and_context(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    path = write_jarn_md(root)
    assert path.name == "JARN.md"
    ctx = assemble_system_context(root)
    assert "Project context" in ctx


def test_write_jarn_md_no_overwrite(tmp_path):
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    write_jarn_md(root)
    with pytest.raises(FileExistsError):
        write_jarn_md(root)


def test_init_template_uses_project_name(tmp_path):
    root = tmp_path / "myproject"
    root.mkdir()
    assert "myproject" in init_template(root)


def test_default_db_path_in_project(tmp_path):
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    db = default_db_path(root)
    assert db == root / ".jarn" / "state.sqlite"


# ---------------------------------------------------------------------------
# /memory dump — P5.B acceptance tests
# ---------------------------------------------------------------------------


@pytest.fixture
def base_config():
    """Minimal in-memory config (no real provider needed for memory tests)."""
    from jarn.config.schema import (
        BudgetConfig,
        Config,
        PermissionMode,
        ProviderConfig,
        ProviderType,
        RoutingConfig,
    )
    return Config(
        default_profile="openrouter",
        permission_mode=PermissionMode.ASK,
        providers={
            "openrouter": ProviderConfig(
                type=ProviderType.OPENROUTER,
                api_key="sk-test",
                base_url="http://localhost:9999/v1",
            ),
        },
        routing=RoutingConfig(
            main="openrouter/anthropic/claude-opus-4-8",
            subagent="openrouter/anthropic/claude-haiku-4-5",
        ),
        budget=BudgetConfig(per_session_usd=1.0, warn_at_pct=80, hard_stop=True),
    )


def test_memory_dump_empty_stores(tmp_path, monkeypatch, base_config):
    """/memory dump works when no memories exist and no context file is present."""
    ctrl = _make_controller(tmp_path, monkeypatch, base_config)
    result = ctrl.handle_command("memory", "dump")
    ctrl.close()
    assert "Memory context dump" in result.text
    assert "Global memory index" in result.text
    assert "Project memory index" in result.text
    assert "Context file" in result.text
    assert "Top-k recall" in result.text


def test_memory_dump_shows_global_index(tmp_path, monkeypatch, base_config):
    """Global MEMORY.md entries appear in /memory dump output."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    # Write a global memory entry
    global_store = MemoryStore.global_store()
    global_store.save(Memory(
        name="Coding style",
        description="Use single quotes",
        body="Always use single quotes in Python.",
        type="project",
    ))

    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    from jarn.tui.controller import Controller
    ctrl = Controller(base_config, root)
    result = ctrl.handle_command("memory", "dump")
    ctrl.close()

    assert "Global memory index" in result.text
    assert "Coding style" in result.text


def test_memory_dump_shows_project_index(tmp_path, monkeypatch, base_config):
    """Project MEMORY.md entries appear in /memory dump output for trusted project."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    project_store = MemoryStore.project_store(root)
    assert project_store is not None
    project_store.save(Memory(
        name="Project convention",
        description="Use pytest",
        body="All tests must use pytest.",
        type="project",
    ))

    from jarn.tui.controller import Controller
    ctrl = Controller(base_config, root, project_trusted=True)
    result = ctrl.handle_command("memory", "dump")
    ctrl.close()

    assert "Project memory index" in result.text
    assert "Project convention" in result.text


def test_memory_dump_shows_context_file(tmp_path, monkeypatch, base_config):
    """Loaded context file (JARN.md) content appears in /memory dump."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    (root / "JARN.md").write_text("# My Project\n\nDo not touch the legacy module.", encoding="utf-8")

    from jarn.tui.controller import Controller
    ctrl = Controller(base_config, root, project_trusted=True)
    result = ctrl.handle_command("memory", "dump")
    ctrl.close()

    assert "JARN.md" in result.text
    assert "Do not touch the legacy module." in result.text


def test_memory_dump_context_alias(tmp_path, monkeypatch, base_config):
    """/memory context is an alias for /memory dump."""
    ctrl = _make_controller(tmp_path, monkeypatch, base_config)
    dump_result = ctrl.handle_command("memory", "dump")
    context_result = ctrl.handle_command("memory", "context")
    ctrl.close()
    # Both must produce the same structure (header present in both)
    assert "Memory context dump" in dump_result.text
    assert "Memory context dump" in context_result.text


def test_memory_dump_untrusted_skips_project(tmp_path, monkeypatch, base_config):
    """Untrusted project omits project memory and context file from /memory dump."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    (root / "JARN.md").write_text("# Secret context\n", encoding="utf-8")

    project_store = MemoryStore.project_store(root)
    assert project_store is not None
    project_store.save(Memory(
        name="Secret",
        description="Hidden",
        body="Should not appear.",
        type="project",
    ))

    from jarn.tui.controller import Controller
    ctrl = Controller(base_config, root, project_trusted=False)
    result = ctrl.handle_command("memory", "dump")
    ctrl.close()

    assert "untrusted" in result.text.lower()
    assert "Secret context" not in result.text
    assert "Should not appear." not in result.text


def test_memory_dump_assembles_all_sources(tmp_path, monkeypatch, base_config):
    """All four sources appear in one /memory dump view."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    # Global memory
    global_store = MemoryStore.global_store()
    global_store.save(Memory(
        name="Global rule",
        description="Always lint",
        body="Run ruff on every change.",
        type="project",
    ))

    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    # Project memory
    project_store = MemoryStore.project_store(root)
    assert project_store is not None
    project_store.save(Memory(
        name="Project rule",
        description="Use uv",
        body="Use uv for dependency management.",
        type="project",
    ))

    # Context file
    (root / "JARN.md").write_text("# Harness project\n\nNo bare except clauses.", encoding="utf-8")

    from jarn.tui.controller import Controller
    ctrl = Controller(base_config, root, project_trusted=True)
    result = ctrl.handle_command("memory", "dump")
    ctrl.close()

    text = result.text
    # All four sections present
    assert "Global memory index" in text
    assert "Project memory index" in text
    assert "JARN.md" in text
    assert "Top-k recall" in text
    # Content from each source
    assert "Global rule" in text
    assert "Project rule" in text
    assert "No bare except clauses." in text


def test_memory_dump_recall_section_surfaces_a_memory(tmp_path, monkeypatch, base_config):
    """The Top-k recall section must contain a real recalled memory, not just a label."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    global_store = MemoryStore.global_store()
    global_store.save(Memory(
        name="Use uv for deps",
        description="Always use uv for dependency management",
        body="Run uv sync / uv add for dependencies.",
        type="project",
    ))

    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    project_store = MemoryStore.project_store(root)
    assert project_store is not None
    project_store.save(Memory(
        name="Run ruff",
        description="Lint every change with ruff",
        body="Run ruff check on all changes.",
        type="project",
    ))

    from jarn.tui.controller import Controller
    ctrl = Controller(base_config, root, project_trusted=True)
    result = ctrl.handle_command("memory", "dump")
    ctrl.close()

    # Isolate the recall section (everything from its header onward) so we assert
    # the recalled memory is named in the recall view itself, not in an index above.
    text = result.text
    recall_section = text[text.index("Top-k recall"):]
    assert "(no memories to recall)" not in recall_section
    assert "Use uv for deps" in recall_section or "Run ruff" in recall_section


def test_memory_crud_unaffected_by_dump(tmp_path, monkeypatch, base_config):
    """Existing /memory CRUD subcommands still work after dump was added."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    from jarn.tui.controller import Controller
    ctrl = Controller(base_config, root, project_trusted=True)

    add_result = ctrl.handle_command(
        "memory", 'add project project "Test mem" "a description" "body text"'
    )
    assert "Saved" in add_result.text

    show_result = ctrl.handle_command("memory", "show project test-mem")
    assert "Test mem" in show_result.text

    search_result = ctrl.handle_command("memory", "search description")
    assert "Test mem" in search_result.text

    update_result = ctrl.handle_command(
        "memory", 'update project "Test mem" "updated description"'
    )
    assert "Updated" in update_result.text

    delete_result = ctrl.handle_command("memory", "delete project test-mem")
    assert "Deleted" in delete_result.text

    ctrl.close()
