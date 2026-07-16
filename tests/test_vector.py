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


def test_provider_embedder_marked_experimental_unwired():
    from jarn.memory.vector import ProviderEmbedder

    doc = (ProviderEmbedder.__doc__ or "").lower()
    assert "experimental" in doc
    assert "unwired" in doc


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


def test_noop_rebuild_does_not_rewrite_cache(tmp_path):
    """An unchanged rebuild (every digest hit) must not touch .vectors.json."""
    store = _store(tmp_path)
    VectorIndex(store).build()
    cache_file = store.root / ".vectors.json"
    assert cache_file.is_file()

    before = cache_file.stat().st_mtime_ns
    # Rebuild with an identical store: nothing changed → no disk write.
    VectorIndex(store).build()
    after = cache_file.stat().st_mtime_ns
    assert after == before, "no-op rebuild must not rewrite the cache file"


def test_rebuild_after_change_rewrites_cache(tmp_path):
    """Adding a memory changes the cache, so the file is rewritten."""
    store = _store(tmp_path)
    VectorIndex(store).build()
    cache_file = store.root / ".vectors.json"
    before = cache_file.stat().st_mtime_ns

    store.save(Memory(name="new-note", description="a fresh note",
                      body="Newly added memory body.", type="project"))
    VectorIndex(store).build()
    after = cache_file.stat().st_mtime_ns
    assert after != before, "a changed store must rewrite the cache file"


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


def test_recall_block_includes_body_excerpt(tmp_path, monkeypatch):
    """Each hit carries an indented body excerpt so recall has real signal."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    MemoryStore.global_store().save(
        Memory(
            name="deploy-flow",
            description="how deployment works",
            body="Deploy via the release workflow and rolling releases.",
            type="project",
        )
    )

    block = recall_block("how does deployment work", include_project=False)

    assert "deploy-flow" in block
    assert "Deploy via the release workflow" in block


def test_recall_block_excerpt_truncated_with_ellipsis(tmp_path, monkeypatch):
    """A body over 400 chars is capped and suffixed with an ellipsis (no more)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    long_body = "postgres " * 200  # well over 400 chars
    MemoryStore.global_store().save(
        Memory(
            name="db-facts",
            description="database facts",
            body=long_body,
            type="reference",
        )
    )

    block = recall_block("postgres database facts", include_project=False)

    assert "…" in block
    # The excerpt line is the raw body capped at 400 chars plus the ellipsis.
    excerpt_line = f"  {long_body.strip()[:400]}…"
    assert excerpt_line in block


def test_recall_block_short_body_has_no_ellipsis(tmp_path, monkeypatch):
    """A short body is emitted verbatim with no ellipsis and no trailing space."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    MemoryStore.global_store().save(
        Memory(
            name="short-note",
            description="a short note",
            body="Neon Postgres.",
            type="reference",
        )
    )

    block = recall_block("which postgres database", include_project=False)

    assert "  Neon Postgres." in block
    assert "…" not in block


def test_recall_block_multiline_body_indents_every_line(tmp_path, monkeypatch):
    """Every excerpt line is indented so continuation lines can't escape the block."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    MemoryStore.global_store().save(
        Memory(
            name="multi",
            description="a multiline note",
            body="first line\nsecond line\n# heading",
            type="reference",
        )
    )

    block = recall_block("multiline note", include_project=False)

    # The heading and continuation lines are part of the excerpt (past the header).
    excerpt_lines = block.splitlines()[2:]
    assert excerpt_lines  # the multiline body produced excerpt rows
    for line in excerpt_lines:
        assert line.startswith("  "), f"escaped to column 0: {line!r}"
        assert not line.startswith("#"), f"heading escaped: {line!r}"
