"""Memory store, project context, and session index tests."""

from __future__ import annotations

import pytest

from jarn.memory.context import assemble_system_context, init_template, write_jarn_md
from jarn.memory.sessions import SessionIndex, default_db_path, new_thread_id
from jarn.memory.store import Memory, MemoryStore, slugify


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
