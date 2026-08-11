"""Memory store, project context, and session index tests."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarn.memory.context import (
    assemble_system_context,
    init_template,
    memory_index_context,
    write_jarn_md,
)
from jarn.memory.sessions import (
    SessionIndex,
    TranscriptWriter,
    UnsafeSessionExportError,
    default_db_path,
    new_thread_id,
)
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
    mem = Memory(
        name="Likes TOML", description="prefers toml", body="The user likes config.", type="user"
    )
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


def test_session_index_tracks_recovery_metadata_and_completion(tmp_path):
    idx = SessionIndex(tmp_path / "state.sqlite")
    idx.touch(
        "thai-thread",
        "แก้ระบบติดตั้ง",
        when=100.0,
        project_root=tmp_path / "โปรเจกต์",
        model="codex_subscription/gpt-test",
    )
    interrupted = idx.latest_incomplete()
    assert interrupted is not None
    assert interrupted.thread_id == "thai-thread"
    assert interrupted.project_root.endswith("โปรเจกต์")
    assert interrupted.model == "codex_subscription/gpt-test"

    assert idx.mark_complete("thai-thread", when=101.0)
    assert idx.latest_incomplete() is None
    assert idx.get("thai-thread").state == "complete"


def test_session_index_migrates_legacy_table_without_data_loss(tmp_path):
    db = tmp_path / "state.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE jarn_sessions "
            "(thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO jarn_sessions VALUES ('old', 'legacy', 1.0)")

    idx = SessionIndex(db)
    old = idx.get("old")
    assert old is not None
    assert old.title == "legacy"
    assert old.state == "complete"


def test_session_export_is_redacted_atomic_and_delete_removes_owned_data(tmp_path):
    idx = SessionIndex(tmp_path / "state.sqlite")
    idx.touch("t1", "secret test", when=1.0)
    transcript = idx.transcript_path("t1")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"type": "user", "text": "token sk-" + "x" * 40}) + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / "exports" / "t1.jsonl"
    assert idx.export("t1", destination) == destination.resolve()
    assert "sk-" + "x" * 40 not in destination.read_text(encoding="utf-8")
    assert idx.delete("t1")
    assert idx.get("t1") is None
    assert not transcript.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs Windows privileges")
def test_session_export_refuses_source_and_destination_symlinks_without_data_loss(
    tmp_path: Path,
) -> None:
    idx = SessionIndex(tmp_path / "state.sqlite")
    idx.touch("safe-export", "safe export", when=1.0)
    transcript = idx.transcript_path("safe-export")
    transcript.parent.mkdir(parents=True)

    outside_source = tmp_path / "outside-source.jsonl"
    outside_source_bytes = b'{"type":"user","text":"outside"}\n'
    outside_source.write_bytes(outside_source_bytes)
    transcript.symlink_to(outside_source)
    refused_output = tmp_path / "must-not-exist.jsonl"

    with pytest.raises(UnsafeSessionExportError, match="symbolic-link transcript"):
        idx.export("safe-export", refused_output)

    assert outside_source.read_bytes() == outside_source_bytes
    assert transcript.is_symlink()
    assert not refused_output.exists()

    transcript.unlink()
    transcript.write_text('{"type":"user","text":"safe"}\n', encoding="utf-8")
    victim = tmp_path / "outside-important.txt"
    victim_bytes = b"DO NOT OVERWRITE\n"
    victim.write_bytes(victim_bytes)
    destination = tmp_path / "export.jsonl"
    destination.symlink_to(victim)

    with pytest.raises(UnsafeSessionExportError, match="export destination"):
        idx.export("safe-export", destination)

    assert victim.read_bytes() == victim_bytes
    assert destination.is_symlink()
    assert not list(tmp_path.glob(".jarn-session-export-*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs Windows privileges")
def test_session_export_refuses_symlinked_source_or_destination_parent(
    tmp_path: Path,
) -> None:
    idx = SessionIndex(tmp_path / "state.sqlite")
    idx.touch("parent-export", "parent export", when=1.0)
    transcript = idx.transcript_path("parent-export")
    outside_sessions = tmp_path / "outside-sessions"
    outside_sessions.mkdir()
    (outside_sessions / transcript.name).write_text(
        '{"type":"user","text":"outside"}\n', encoding="utf-8"
    )
    transcript.parent.symlink_to(outside_sessions)

    with pytest.raises(UnsafeSessionExportError, match="transcript directory"):
        idx.export("parent-export", tmp_path / "unused.jsonl")

    transcript.parent.unlink()
    transcript.parent.mkdir()
    transcript.write_text('{"type":"user","text":"safe"}\n', encoding="utf-8")
    outside_exports = tmp_path / "outside-exports"
    outside_exports.mkdir()
    victim = outside_exports / "victim.jsonl"
    victim_bytes = b"PRESERVE ME\n"
    victim.write_bytes(victim_bytes)
    linked_parent = tmp_path / "linked-exports"
    linked_parent.symlink_to(outside_exports, target_is_directory=True)

    with pytest.raises(UnsafeSessionExportError, match="export parent"):
        idx.export("parent-export", linked_parent / "victim.jsonl")

    assert victim.read_bytes() == victim_bytes
    assert linked_parent.is_symlink()


def _seed_session_delete_rows(db: Path, thread_id: str) -> None:
    with sqlite3.connect(db) as conn:
        for table in ("checkpoint_writes", "writes", "checkpoint_blobs", "checkpoints"):
            conn.execute(f"CREATE TABLE {table} (thread_id TEXT, payload TEXT)")
            conn.execute(
                f"INSERT INTO {table} (thread_id, payload) VALUES (?, 'keep')",
                (thread_id,),
            )


def _session_delete_row_counts(db: Path, thread_id: str) -> dict[str, int]:
    with sqlite3.connect(db) as conn:
        return {
            table: int(
                conn.execute(
                    f"SELECT count(*) FROM {table} WHERE thread_id=?", (thread_id,)
                ).fetchone()[0]
            )
            for table in ("checkpoint_writes", "writes", "checkpoint_blobs", "checkpoints")
        }


def test_session_delete_unlink_failure_restores_transcript_and_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "state.sqlite"
    idx = SessionIndex(db)
    idx.touch("retry-me", "retry deletion", when=1.0)
    transcript = idx.transcript_path("retry-me")
    transcript.parent.mkdir(parents=True)
    original = b'{"type":"user","text":"keep me retryable"}\n'
    transcript.write_bytes(original)
    _seed_session_delete_rows(db, "retry-me")
    real_unlink = Path.unlink

    def fail_tombstone_unlink(path: Path, *args, **kwargs) -> None:
        if path.name == ".retry-me.jsonl.delete-pending":
            raise PermissionError("injected unlink refusal")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_tombstone_unlink)

    assert idx.delete("retry-me") is False
    assert idx.get("retry-me") is not None
    assert transcript.read_bytes() == original
    assert not transcript.with_name(".retry-me.jsonl.delete-pending").exists()
    assert _session_delete_row_counts(db, "retry-me") == {
        "checkpoint_writes": 1,
        "writes": 1,
        "checkpoint_blobs": 1,
        "checkpoints": 1,
    }

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert idx.delete("retry-me") is True
    assert idx.get("retry-me") is None
    assert not transcript.exists()
    assert _session_delete_row_counts(db, "retry-me") == {
        "checkpoint_writes": 0,
        "writes": 0,
        "checkpoint_blobs": 0,
        "checkpoints": 0,
    }


def test_session_delete_staging_failure_never_deletes_database_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "state.sqlite"
    idx = SessionIndex(db)
    idx.touch("stage-me", "stage deletion", when=1.0)
    transcript = idx.transcript_path("stage-me")
    transcript.parent.mkdir(parents=True)
    transcript.write_text("one durable line\n", encoding="utf-8")
    _seed_session_delete_rows(db, "stage-me")
    real_replace = os.replace

    def fail_staging(source, destination) -> None:
        if Path(source) == transcript and Path(destination).name.endswith(".delete-pending"):
            raise PermissionError("injected rename refusal")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_staging)

    assert idx.delete("stage-me") is False
    assert idx.get("stage-me") is not None
    assert transcript.read_text(encoding="utf-8") == "one durable line\n"
    assert _session_delete_row_counts(db, "stage-me") == {
        "checkpoint_writes": 1,
        "writes": 1,
        "checkpoint_blobs": 1,
        "checkpoints": 1,
    }


def test_session_delete_recovers_staged_transcript_after_interrupted_attempt(
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.sqlite"
    idx = SessionIndex(db)
    idx.touch("recover-me", "recover deletion", when=1.0)
    transcript = idx.transcript_path("recover-me")
    transcript.parent.mkdir(parents=True)
    transcript.write_text("recoverable\n", encoding="utf-8")
    tombstone = transcript.with_name(".recover-me.jsonl.delete-pending")
    os.replace(transcript, tombstone)

    assert idx.delete("recover-me") is True
    assert idx.get("recover-me") is None
    assert not transcript.exists()
    assert not tombstone.exists()


def test_session_delete_refuses_live_cross_process_writer_then_removes_all_data(
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.sqlite"
    idx = SessionIndex(db)
    idx.touch("active-writer", "live session", when=1.0)
    _seed_session_delete_rows(db, "active-writer")
    sessions_dir = idx.transcript_path("active-writer").parent
    ready = tmp_path / "writer.ready"
    release = tmp_path / "writer.release"
    child_code = """
import sys
import time
from pathlib import Path
from jarn.memory.sessions import TranscriptWriter

sessions_dir, ready, release = map(Path, sys.argv[1:])
writer = TranscriptWriter("active-writer", sessions_dir=sessions_dir)
writer.append({"type": "user", "text": "held by child"})
ready.write_text("ready", encoding="utf-8")
while not release.exists():
    time.sleep(0.01)
writer.close()
"""
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter/test fixture
        [sys.executable, "-c", child_code, str(sessions_dir), str(ready), str(release)],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("child transcript writer did not become ready")
            time.sleep(0.01)
        if process.poll() is not None:
            pytest.fail(f"child transcript writer exited early: {process.stderr.read()}")

        started = time.monotonic()
        assert idx.delete("active-writer") is False
        assert time.monotonic() - started < 1.0
        assert idx.get("active-writer") is not None
        assert idx.transcript_path("active-writer").read_text(encoding="utf-8") == (
            '{"type": "user", "text": "held by child"}\n'
        )
        assert _session_delete_row_counts(db, "active-writer") == {
            "checkpoint_writes": 1,
            "writes": 1,
            "checkpoint_blobs": 1,
            "checkpoints": 1,
        }
    finally:
        release.write_text("close", encoding="utf-8")
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)

    stderr = process.stderr.read() if process.stderr is not None else ""
    assert process.returncode == 0, stderr
    assert idx.delete("active-writer") is True
    assert idx.get("active-writer") is None
    assert not idx.transcript_path("active-writer").exists()
    assert _session_delete_row_counts(db, "active-writer") == {
        "checkpoint_writes": 0,
        "writes": 0,
        "checkpoint_blobs": 0,
        "checkpoints": 0,
    }


def test_session_delete_refuses_live_in_process_writer_without_waiting(
    tmp_path: Path,
) -> None:
    idx = SessionIndex(tmp_path / "state.sqlite")
    idx.touch("same-process", "live session", when=1.0)
    writer = TranscriptWriter(
        "same-process",
        sessions_dir=idx.transcript_path("same-process").parent,
    )
    writer.write_user("still active", ts=1.0)

    started = time.monotonic()
    try:
        assert idx.delete("same-process") is False
        assert time.monotonic() - started < 1.0
        assert idx.get("same-process") is not None
        assert idx.transcript_path("same-process").is_file()
    finally:
        writer.close()

    assert idx.delete("same-process") is True
    assert idx.get("same-process") is None
    assert not idx.transcript_path("same-process").exists()


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


def test_memory_indices_share_one_prompt_budget(tmp_path, monkeypatch):
    from jarn.memory.tokens import count_tokens

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    global_store = MemoryStore.global_store()
    project_store = MemoryStore.project_store(root)
    assert project_store is not None
    for i in range(12):
        description = (f"entry {i} detailed context " * 10).strip()
        global_store.save(Memory(f"Global {i}", description, "body"))
        project_store.save(Memory(f"Project {i}", description, "body"))

    block = memory_index_context(root, token_budget=240)

    assert count_tokens(block) <= 240
    assert "Long-term memory (global)" in block
    assert "Long-term memory (project)" in block
    assert "truncated" in block


def test_memory_budget_redistributes_unused_tier_capacity(tmp_path, monkeypatch):
    from jarn.memory.tokens import count_tokens

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    global_store = MemoryStore.global_store()
    project_store = MemoryStore.project_store(root)
    assert project_store is not None
    global_store.save(Memory("Short", "tiny", "body"))
    for i in range(20):
        project_store.save(
            Memory(
                f"Project {i}",
                ("detailed project context " * 12).strip(),
                "body",
            )
        )

    block = memory_index_context(root, token_budget=220)
    sections = block.split("\n\n---\n\n")

    assert count_tokens(block) <= 220
    assert len(sections) == 2
    assert count_tokens(sections[1]) > 110  # reclaimed the short global tier's slack


def test_truncated_project_context_routes_to_full_file(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "AGENTS.md").write_text(
        "guidance\n" * 500,
        encoding="utf-8",
    )

    block = assemble_system_context(
        root,
        context_files=["docs/AGENTS.md"],
        project_context_tokens=40,
    )

    assert "Project context (docs/AGENTS.md)" in block
    assert "More guidance exists in `docs/AGENTS.md`" in block
    assert "Read that file" in block


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
    global_store.save(
        Memory(
            name="Coding style",
            description="Use single quotes",
            body="Always use single quotes in Python.",
            type="project",
        )
    )

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
    project_store.save(
        Memory(
            name="Project convention",
            description="Use pytest",
            body="All tests must use pytest.",
            type="project",
        )
    )

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
    (root / "JARN.md").write_text(
        "# My Project\n\nDo not touch the legacy module.", encoding="utf-8"
    )

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
    project_store.save(
        Memory(
            name="Secret",
            description="Hidden",
            body="Should not appear.",
            type="project",
        )
    )

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
    global_store.save(
        Memory(
            name="Global rule",
            description="Always lint",
            body="Run ruff on every change.",
            type="project",
        )
    )

    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)

    # Project memory
    project_store = MemoryStore.project_store(root)
    assert project_store is not None
    project_store.save(
        Memory(
            name="Project rule",
            description="Use uv",
            body="Use uv for dependency management.",
            type="project",
        )
    )

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
    global_store.save(
        Memory(
            name="Use uv for deps",
            description="Always use uv for dependency management",
            body="Run uv sync / uv add for dependencies.",
            type="project",
        )
    )

    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    project_store = MemoryStore.project_store(root)
    assert project_store is not None
    project_store.save(
        Memory(
            name="Run ruff",
            description="Lint every change with ruff",
            body="Run ruff check on all changes.",
            type="project",
        )
    )

    from jarn.tui.controller import Controller

    ctrl = Controller(base_config, root, project_trusted=True)
    result = ctrl.handle_command("memory", "dump")
    ctrl.close()

    # Isolate the recall section (everything from its header onward) so we assert
    # the recalled memory is named in the recall view itself, not in an index above.
    text = result.text
    recall_section = text[text.index("Top-k recall") :]
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

    update_result = ctrl.handle_command("memory", 'update project "Test mem" "updated description"')
    assert "Updated" in update_result.text

    delete_result = ctrl.handle_command("memory", "delete project test-mem")
    assert "Deleted" in delete_result.text

    ctrl.close()


def test_index_budget(tmp_path):
    """A huge MEMORY.md index is truncated to the configured token budget."""
    from jarn.memory.store import MemoryStore
    from jarn.memory.tokens import count_tokens

    store = MemoryStore(tmp_path / "memory")
    for i in range(200):
        store.save(
            Memory(
                name=f"Memory {i}",
                description="x" * 200,
                body="body",
                type="project",
            )
        )
    index = store.index_text(token_budget=100)
    assert "(truncated" in index
    assert count_tokens(index) <= 110  # small slack for notice overhead
