"""Integration guard: auto-checkpoint and the JSONL transcript must be wired
onto the driver that actually runs turns.

Both features shipped their components (CheckpointManager, TranscriptWriter) and
the SessionDriver fields, but a turn only exercises them if make_driver connects
them and run_turn calls them. These tests fail if that wiring regresses — the
component-level unit tests cannot catch a missing connection.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarn.agent.checkpoint import CheckpointManager
from jarn.agent.session import EventKind, SessionDriver
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.memory.sessions import make_transcript_writer
from jarn.permissions import PermissionEngine
from jarn.tui.controller import Controller


class _FakeAIChunk:
    type = "ai"

    def __init__(self, content: str = "") -> None:
        self.content = content
        self.usage_metadata = {"input_tokens": 1, "output_tokens": 1}
        self.response_metadata: dict = {}


class _OnePassAgent:
    """A single astream pass that completes — no tools, no interrupts."""

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        yield ("messages", (_FakeAIChunk("ok"),))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("orig\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    (repo / ".jarn").mkdir()


@pytest.mark.asyncio
async def test_run_turn_calls_checkpoint_snapshot_and_writes_transcript(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    mgr = CheckpointManager(repo_root=repo, enabled=True)
    snap_calls: list[str] = []
    _orig = mgr.snapshot

    def _spy(label: str, *, now: float | None = None, thread_id=None, turn_index=None):
        snap_calls.append(label)
        return _orig(label, now=now, thread_id=thread_id, turn_index=turn_index)

    mgr.snapshot = _spy  # type: ignore[method-assign]

    writer = make_transcript_writer("sess-x", project_root=repo)
    driver = SessionDriver(
        agent=_OnePassAgent(),
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="sess-x",
        main_model_ref="m",
        checkpoint=mgr,
        transcript=writer,
    )

    events = [ev async for ev in driver.run_turn("please edit the file")]

    assert any(e.kind is EventKind.DONE for e in events)
    # run_turn must have snapshotted before the agent could touch files.
    assert snap_calls, "run_turn did not call checkpoint.snapshot()"
    # transcript must have a user event for the turn.
    tfile = repo / ".jarn" / "sessions" / "sess-x.jsonl"
    assert tfile.exists(), "transcript file was not written"
    events_jsonl = [json.loads(line) for line in tfile.read_text().splitlines()]
    assert any(e.get("type") == "user" for e in events_jsonl)


def test_make_driver_wires_transcript_and_checkpoint(tmp_path, monkeypatch, base_config):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    base_config.observability.transcript = True
    base_config.git.autocheckpoint = True

    ctrl = Controller(base_config, root)
    # Stub a runtime so make_driver can build a driver without a real model.
    ctrl.runtime = SimpleNamespace(agent=object(), main_model_ref="m", known_model_refs=())

    driver = ctrl.make_driver(approver=lambda *a, **k: None)
    assert driver.transcript is not None, "transcript not wired onto the driver"
    assert driver.checkpoint is ctrl.checkpoint_manager, "checkpoint not wired onto the driver"
    ctrl.close()


def test_make_driver_skips_transcript_when_disabled(tmp_path, monkeypatch, base_config):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    base_config.observability.transcript = False

    ctrl = Controller(base_config, root)
    ctrl.runtime = SimpleNamespace(agent=object(), main_model_ref="m", known_model_refs=())

    driver = ctrl.make_driver(approver=lambda *a, **k: None)
    assert driver.transcript is None, "transcript should be off when disabled"
    ctrl.close()
