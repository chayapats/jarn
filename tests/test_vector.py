"""Semantic recall (vector index) tests — fully offline via LocalEmbedder."""

from __future__ import annotations

from jarn.memory.store import Memory, MemoryStore
from jarn.memory.vector import LocalEmbedder, VectorIndex, recall_block


def _store(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save(Memory(name="prefers-pytest", description="user likes pytest for testing",
                      body="Always use pytest and parametrized tests.", type="user"))
    store.save(Memory(name="deploy-flow", description="how deployment works",
                      body="Deploy via the release workflow and rolling releases.", type="project"))
    store.save(Memory(name="db-choice", description="database is postgres on neon",
                      body="The project uses Neon Postgres through the marketplace.", type="project"))
    return store


def test_local_embedder_deterministic():
    e = LocalEmbedder()
    assert e.embed("hello world") == e.embed("hello world")
    assert len(e.embed("hello")) == 256


def test_local_embedder_normalized():
    import math
    v = LocalEmbedder().embed("some text here")
    assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6


def test_search_ranks_relevant_first(tmp_path):
    index = VectorIndex(_store(tmp_path))
    hits = index.search("which database does the project use", k=3)
    assert hits
    assert hits[0].memory.name == "db-choice"


def test_search_testing_query(tmp_path):
    index = VectorIndex(_store(tmp_path))
    hits = index.search("how should I write tests", k=2)
    assert hits[0].memory.name == "prefers-pytest"


def test_cache_reused(tmp_path):
    store = _store(tmp_path)
    index = VectorIndex(store)
    assert index.build() == 3
    assert (store.root / ".vectors.json").is_file()
    # Rebuild uses cache (no error, same count)
    assert VectorIndex(store).build() == 3


def test_empty_store_search(tmp_path):
    index = VectorIndex(MemoryStore(tmp_path / "empty"))
    assert index.search("anything") == []


def test_recall_block_dedupes_global_and_project(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    MemoryStore.global_store().save(
        Memory(
            name="prefers-pytest",
            description="global pytest preference",
            body="Use pytest for tests.",
            type="user",
        )
    )
    project = MemoryStore.project_store(root)
    assert project is not None
    project.save(
        Memory(
            name="prefers-pytest",
            description="project pytest preference",
            body="Use pytest fixtures in this project.",
            type="project",
        )
    )

    block = recall_block("how should I write pytest tests", project_root=root)

    assert block.count("prefers-pytest") == 1


def test_recall_block_can_skip_project_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    project = MemoryStore.project_store(root)
    assert project is not None
    project.save(
        Memory(
            name="project-db",
            description="project uses neon postgres",
            body="The project database is Neon Postgres.",
            type="project",
        )
    )

    block = recall_block("which postgres database is used", project_root=root, include_project=False)

    assert "project-db" not in block
