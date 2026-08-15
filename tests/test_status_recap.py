"""Local /status Recap from session transcript JSONL + cost tracker."""

from __future__ import annotations

import json
from pathlib import Path

from jarn.controller.commands.diagnostics import _transcript_recap
from jarn.tui.controller import Controller


def _controller(tmp_path, monkeypatch, base_config):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    return Controller(base_config, root)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_transcript_recap_reads_user_write_and_assistant(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [
            {"ts": 1.0, "type": "user", "text": "Fix the flaky toolbar test"},
            {
                "ts": 2.0,
                "type": "tool",
                "name": "write_file",
                "args": {"file_path": "src/jarn/tui/toolbar.py"},
            },
            {"ts": 3.0, "type": "assistant", "text": "Patched the priority sort"},
        ],
    )
    recap = _transcript_recap(path)
    assert recap["turns"] == 1
    assert recap["last_user"] == "Fix the flaky toolbar test"
    assert recap["last_assistant"] == "Patched the priority sort"
    assert recap["files"] == ["src/jarn/tui/toolbar.py"]


def test_transcript_recap_ignores_steered_and_verification_users(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [
            {"ts": 1.0, "type": "user", "text": "real question"},
            {"ts": 2.0, "type": "user", "text": "(steered) switch to pathlib"},
            {"ts": 3.0, "type": "user", "text": "(verification repair) rerun the test"},
            {"ts": 4.0, "type": "assistant", "text": "ok"},
        ],
    )
    recap = _transcript_recap(path)
    assert recap["turns"] == 1
    assert recap["last_user"] == "real question"
    assert recap["last_assistant"] == "ok"


def test_transcript_recap_missing_or_empty_is_zero(tmp_path: Path) -> None:
    missing = _transcript_recap(tmp_path / "absent.jsonl")
    assert missing["turns"] == 0
    assert missing["last_user"] == ""
    assert missing["last_assistant"] == ""
    assert missing["files"] == []

    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("", encoding="utf-8")
    empty = _transcript_recap(empty_path)
    assert empty["turns"] == 0
    assert empty["last_user"] == ""
    assert empty["last_assistant"] == ""
    assert empty["files"] == []


def test_transcript_recap_skips_malformed_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        '{"type": "user", "text": "hello"}\n'
        "this is not json\n"
        '{"type": "assistant", "text": "hi"}\n',
        encoding="utf-8",
    )
    recap = _transcript_recap(path)
    assert recap["turns"] == 1
    assert recap["last_user"] == "hello"
    assert recap["last_assistant"] == "hi"


def test_status_recap_integration_includes_transcript_and_tracker(
    tmp_path, monkeypatch, base_config
) -> None:
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    transcript = ctrl.sessions.transcript_path(ctrl.thread_id)
    _write_jsonl(
        transcript,
        [
            {"ts": 1.0, "type": "user", "text": "Fix the flaky toolbar test"},
            {
                "ts": 2.0,
                "type": "tool",
                "name": "write_file",
                "args": {"file_path": "src/jarn/tui/toolbar.py"},
            },
            {
                "ts": 3.0,
                "type": "tool",
                "name": "edit_file",
                "args": {"path": "tests/test_toolbar.py"},
            },
            {
                "ts": 4.0,
                "type": "assistant",
                "text": "Patched the priority sort so the cost segment can be kept",
            },
        ],
    )
    ctrl.tracker.record("openrouter/test", 10, 10, tool="write_file")
    text = ctrl.handle_command("status", "").text
    assert "Directory" in text
    assert "Recap" in text
    assert "Files" in text
    assert "src/jarn/tui/toolbar.py" in text
    assert "Last you" in text
    assert "Last J.A.R.N." in text
    assert "Fix the flaky toolbar test" in text
    assert "Patched the priority sort" in text
    assert "turn" in text
    assert "write_file 1" in text
    ctrl.close()


def test_status_recap_omits_response_bucket(tmp_path, monkeypatch, base_config) -> None:
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    ctrl.tracker.record("openrouter/test", 10, 10)
    text = ctrl.handle_command("status", "").text
    assert "(response)" not in text
    assert "Calls" in text
    ctrl.close()


def test_resume_recap_reuses_status_scan_without_model(
    tmp_path, monkeypatch, base_config
) -> None:
    """Resume recap is local: directory/model/mode + last-turn, no LLM."""
    from jarn.controller.commands.diagnostics import format_resume_recap

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    transcript = ctrl.sessions.transcript_path(ctrl.thread_id)
    _write_jsonl(
        transcript,
        [
            {"ts": 1.0, "type": "user", "text": "Fix the flaky toolbar test"},
            {
                "ts": 2.0,
                "type": "tool",
                "name": "write_file",
                "args": {"file_path": "src/jarn/tui/toolbar.py"},
            },
            {"ts": 3.0, "type": "assistant", "text": "Patched the priority sort"},
        ],
    )
    assert ctrl.runtime is None
    text = format_resume_recap(ctrl)
    assert "Resumed" in text
    assert "Directory" in text
    assert str(ctrl.project_root) in text
    assert "Model" in text
    assert "Permissions" in text
    assert "Last you" in text
    assert "Fix the flaky toolbar test" in text
    assert "Last J.A.R.N." in text
    assert "Patched the priority sort" in text
    assert ctrl.runtime is None
    ctrl.close()
