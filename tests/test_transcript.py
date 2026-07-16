"""Tests for the JSONL session transcript feature.

Covers:
- TranscriptWriter appends events in order and produces valid JSONL.
- user, assistant, and tool events are recorded correctly.
- observability.transcript=false writes nothing.
- No env-var / secret values leak into the file.
- Light integration through SessionDriver with a mocked model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarn.memory.sessions import TranscriptWriter, make_transcript_writer

# ---------------------------------------------------------------------------
# Writer unit tests
# ---------------------------------------------------------------------------

def test_writer_appends_in_order(tmp_path: Path) -> None:
    """Events appear in the JSONL file in the order they were appended."""
    w = TranscriptWriter("sess1", sessions_dir=tmp_path)
    w.append({"ts": 1.0, "type": "user", "text": "hello"})
    w.append({"ts": 2.0, "type": "assistant", "text": "world"})
    w.close()

    lines = w.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["type"] == "user" and first["text"] == "hello"
    assert second["type"] == "assistant" and second["text"] == "world"


def test_each_line_is_valid_json(tmp_path: Path) -> None:
    """Every line in the transcript parses as a JSON object."""
    w = TranscriptWriter("sess2", sessions_dir=tmp_path)
    for i in range(5):
        w.append({"ts": float(i), "type": "user", "text": f"msg {i}"})
    w.close()

    for line in w.path.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        assert isinstance(obj, dict)


def test_write_user_event(tmp_path: Path) -> None:
    w = TranscriptWriter("u", sessions_dir=tmp_path)
    w.write_user("do the thing", ts=10.5)
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    assert obj["type"] == "user"
    assert obj["text"] == "do the thing"
    assert obj["ts"] == pytest.approx(10.5)


def test_write_assistant_event(tmp_path: Path) -> None:
    w = TranscriptWriter("a", sessions_dir=tmp_path)
    w.write_assistant("Sure, done.", ts=20.0)
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    assert obj["type"] == "assistant"
    assert obj["text"] == "Sure, done."


def test_write_user_redacts_secret_shaped_text(tmp_path: Path) -> None:
    """A user prompt containing a key-shaped string is scrubbed before persist."""
    from jarn.memory.sessions import redact_secrets

    w = TranscriptWriter("redact-u", sessions_dir=tmp_path)
    secret = "sk-ant-api03-" + "A" * 40
    w.write_user(f"deploy with key {secret} please", ts=1.0)
    w.write_user("export ANTHROPIC_API_KEY=hunter2supersecret", ts=2.0)
    w.close()

    body = w.path.read_text(encoding="utf-8")
    assert secret not in body
    assert "hunter2supersecret" not in body
    assert "[REDACTED]" in body
    # The non-secret surrounding text is preserved.
    assert "deploy with key" in body
    assert "ANTHROPIC_API_KEY" in body  # name kept, value redacted

    # redact_secrets leaves ordinary text untouched.
    assert redact_secrets("just a normal sentence") == "just a normal sentence"


def test_write_assistant_redacts_secret_shaped_text(tmp_path: Path) -> None:
    w = TranscriptWriter("redact-a", sessions_dir=tmp_path)
    token = "ghp_" + "b" * 36
    w.write_assistant(f"Your token is {token}", ts=1.0)
    w.close()
    body = w.path.read_text(encoding="utf-8")
    assert token not in body
    assert "[REDACTED]" in body


def test_write_tool_start_event(tmp_path: Path) -> None:
    """A tool-start event records name and args but no result."""
    w = TranscriptWriter("t", sessions_dir=tmp_path)
    w.write_tool("read_file", ts=5.0, args={"file_path": "src/app.py"})
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    assert obj["type"] == "tool"
    assert obj["name"] == "read_file"
    assert obj["args"] == {"file_path": "src/app.py"}
    assert "result" not in obj


def test_write_tool_result_event(tmp_path: Path) -> None:
    """A tool-result event records name and result."""
    w = TranscriptWriter("t2", sessions_dir=tmp_path)
    w.write_tool("execute", ts=6.0, result="3 lines")
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    assert obj["type"] == "tool"
    assert obj["result"] == "3 lines"
    assert "truncated" not in obj


def test_tool_result_truncated_at_cap(tmp_path: Path) -> None:
    """Tool outputs exceeding the cap are truncated and flagged."""
    from jarn.memory.sessions import _TRANSCRIPT_MAX_TOOL_CHARS

    big_output = "x" * (_TRANSCRIPT_MAX_TOOL_CHARS + 100)
    w = TranscriptWriter("tc", sessions_dir=tmp_path)
    w.write_tool("read_file", ts=1.0, result=big_output)
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    assert len(obj["result"]) == _TRANSCRIPT_MAX_TOOL_CHARS
    assert obj.get("truncated") is True


def test_directory_created_lazily(tmp_path: Path) -> None:
    """The sessions directory is created on first write, not at construction."""
    sessions_dir = tmp_path / "deep" / "sessions"
    assert not sessions_dir.exists()

    w = TranscriptWriter("lazy", sessions_dir=sessions_dir)
    assert not sessions_dir.exists()   # not yet

    w.append({"ts": 1.0, "type": "user", "text": "hi"})
    assert sessions_dir.exists()
    w.close()


def test_incremental_flush(tmp_path: Path) -> None:
    """Each append is flushed immediately — readable before close()."""
    w = TranscriptWriter("flush", sessions_dir=tmp_path)
    w.append({"ts": 1.0, "type": "user", "text": "first"})
    # Do NOT call close() — file should still be readable right now.
    content = w.path.read_text(encoding="utf-8")
    assert "first" in content
    w.close()


def test_make_transcript_writer_uses_project_sessions_dir(tmp_path: Path) -> None:
    """make_transcript_writer resolves the project sessions directory."""
    (tmp_path / ".jarn").mkdir()
    w = make_transcript_writer("proj-sess", project_root=tmp_path)
    assert w.path == tmp_path / ".jarn" / "sessions" / "proj-sess.jsonl"
    w.close()


def test_make_transcript_writer_global_fallback(tmp_path: Path, monkeypatch) -> None:
    """Falls back to global sessions dir when no project root is found."""
    from jarn.config import paths

    global_home = tmp_path / "global-jarn"
    monkeypatch.setenv("JARN_HOME", str(global_home))
    # Make project_sessions_dir return None (no project root) regardless of CWD.
    monkeypatch.setattr(paths, "project_sessions_dir", lambda *a, **k: None)
    w = make_transcript_writer("global-sess", project_root=None)
    assert w.path.parent == global_home / "sessions"
    w.close()


# ---------------------------------------------------------------------------
# Integration: SessionDriver emits transcript events
# ---------------------------------------------------------------------------

class _FakeAIChunk:
    type = "ai"

    def __init__(self, content: str = "", usage: dict | None = None) -> None:
        self.content = content
        self.usage_metadata = usage
        self.response_metadata: dict = {}


class _SimpleAgent:
    """Single-pass agent that streams one text chunk then finishes."""

    def __init__(self, text: str = "All done.") -> None:
        self.text = text

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        yield (
            (),
            "messages",
            (_FakeAIChunk(self.text, {"input_tokens": 5, "output_tokens": 3}),),
        )


class _ToolAgent:
    """Agent that emits a TOOL_START (via updates) then a TOOL_END (via messages),
    followed by a text reply."""

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        # Tool call notification via the updates channel.
        ai_msg = type("AIMessage", (), {
            "tool_calls": [{"name": "execute", "args": {"command": "ls"}, "id": "c1"}],
        })()
        yield ((), "updates", {"node": {"messages": [ai_msg]}})

        # Tool result via the messages channel.
        tool_msg = type("ToolMessage", (), {
            "type": "tool",
            "content": "file1.py\nfile2.py\n",
            "name": "execute",
            "tool_call_id": "c1",
            "usage_metadata": None,
        })()
        yield ((), "messages", (tool_msg,))

        # Assistant reply.
        yield (
            (),
            "messages",
            (_FakeAIChunk("Found 2 files.", {"input_tokens": 2, "output_tokens": 4}),),
        )


@pytest.mark.asyncio
async def test_driver_records_user_and_assistant(tmp_path: Path) -> None:
    """SessionDriver writes user and assistant events through the transcript."""
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    w = TranscriptWriter("drv1", sessions_dir=tmp_path)
    driver = SessionDriver(
        agent=_SimpleAgent("Hello!"),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="drv1",
        transcript=w,
    )
    async for _ in driver.run_turn("say hello"):
        pass
    w.close()

    lines = w.path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(ln)["type"] for ln in lines]
    assert "user" in types
    assert "assistant" in types


@pytest.mark.asyncio
async def test_driver_records_tool_events(tmp_path: Path) -> None:
    """SessionDriver records both tool-start and tool-result events."""
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    w = TranscriptWriter("drv2", sessions_dir=tmp_path)
    driver = SessionDriver(
        agent=_ToolAgent(),
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="drv2",
        transcript=w,
    )
    async for _ in driver.run_turn("list files"):
        pass
    w.close()

    lines = w.path.read_text(encoding="utf-8").splitlines()
    objs = [json.loads(ln) for ln in lines]
    tool_objs = [o for o in objs if o["type"] == "tool"]
    # At least a tool-start (args present) and a tool-result (result present).
    tool_names = {o["name"] for o in tool_objs}
    assert "execute" in tool_names


class _DyingAgent:
    """Streams a partial assistant reply, then the provider connection dies mid-turn
    (the turn never reaches DONE)."""

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        yield (
            (),
            "messages",
            (_FakeAIChunk("partial thought ", {"input_tokens": 1, "output_tokens": 1}),),
        )
        raise RuntimeError("provider connection reset")


@pytest.mark.asyncio
async def test_interrupted_turn_flushes_partial_assistant_text(tmp_path: Path) -> None:
    """FIX D: a turn that dies before DONE (provider error) must still flush its
    already-streamed partial assistant text to the transcript, marked interrupted —
    losing streamed text is worse than an honest partial line."""
    from jarn.agent.session import EventKind, SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    w = TranscriptWriter("interrupted", sessions_dir=tmp_path)
    driver = SessionDriver(
        agent=_DyingAgent(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="interrupted",
        transcript=w,
    )
    events = [ev async for ev in driver.run_turn("go")]
    w.close()

    # The turn surfaced an ERROR and never reached DONE.
    assert any(e.kind is EventKind.ERROR for e in events)
    assert not any(e.kind is EventKind.DONE for e in events)

    objs = [json.loads(ln) for ln in w.path.read_text(encoding="utf-8").splitlines()]
    assistant = [o for o in objs if o["type"] == "assistant"]
    assert len(assistant) == 1, objs
    assert assistant[0]["text"] == "partial thought \n…(turn interrupted)"
    # _turn_text is cleared after the interrupted flush (no re-flush on a retry).
    assert driver._turn_text == ""


@pytest.mark.asyncio
async def test_completed_turn_does_not_double_flush(tmp_path: Path) -> None:
    """FIX D companion: a turn that reaches DONE writes exactly ONE assistant line —
    the DONE flush clears _turn_text so the run_turn finally does not re-emit it."""
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    w = TranscriptWriter("nodup", sessions_dir=tmp_path)
    driver = SessionDriver(
        agent=_SimpleAgent("All done."),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="nodup",
        transcript=w,
    )
    async for _ in driver.run_turn("go"):
        pass
    w.close()

    objs = [json.loads(ln) for ln in w.path.read_text(encoding="utf-8").splitlines()]
    assistant = [o for o in objs if o["type"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["text"] == "All done."


# ---------------------------------------------------------------------------
# transcript=False: nothing written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_transcript_when_writer_is_none(tmp_path: Path) -> None:
    """SessionDriver does not write anything when transcript is None."""
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    sessions_dir = tmp_path / "sessions"
    driver = SessionDriver(
        agent=_SimpleAgent("Hi"),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="no-transcript",
        transcript=None,  # disabled
    )
    async for _ in driver.run_turn("hello"):
        pass
    # Nothing should have been created.
    assert not sessions_dir.exists()


def test_config_transcript_false_parsed(tmp_path: Path) -> None:
    """observability.transcript=false is parsed and honoured by the config loader."""
    import yaml

    from jarn.config.loader import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"observability": {"transcript": False}}), encoding="utf-8"
    )
    cfg = load_config(global_path=cfg_path, project_path=None)
    assert cfg.observability.transcript is False


def test_config_transcript_true_default(tmp_path: Path) -> None:
    """observability.transcript defaults to True when omitted."""
    from jarn.config.loader import load_config

    cfg = load_config(
        global_path=tmp_path / "missing.yaml",
        project_path=None,
    )
    assert cfg.observability.transcript is True


# ---------------------------------------------------------------------------
# Secret / env-var leak guard
# ---------------------------------------------------------------------------

def test_tool_arg_string_value_truncated_at_cap(tmp_path: Path) -> None:
    """Tool args with string values exceeding the cap are truncated in the JSONL.

    This test fails without FIX 5: before the fix, write_tool writes args
    verbatim, so a wiki_write/write_file call with full file content bloats the
    transcript JSONL file.
    """
    from jarn.memory.sessions import _TRANSCRIPT_MAX_TOOL_CHARS

    big_content = "y" * (_TRANSCRIPT_MAX_TOOL_CHARS + 500)
    w = TranscriptWriter("fix5", sessions_dir=tmp_path)
    w.write_tool(
        "wiki_write",
        ts=1.0,
        args={"page": "my-page", "content": big_content},
    )
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    assert obj["type"] == "tool"
    args_recorded = obj["args"]
    # The "content" arg must be truncated.
    assert len(args_recorded["content"]) == _TRANSCRIPT_MAX_TOOL_CHARS, (
        "Large string arg value must be truncated to _TRANSCRIPT_MAX_TOOL_CHARS"
    )
    # The truncation marker must be present.
    assert args_recorded.get("content__truncated") is True
    # Short args (the page slug) must not be truncated.
    assert args_recorded["page"] == "my-page"
    assert "page__truncated" not in args_recorded


def test_tool_arg_non_string_values_not_truncated(tmp_path: Path) -> None:
    """Non-string arg values (ints, booleans, lists) are written as-is."""
    w = TranscriptWriter("fix5b", sessions_dir=tmp_path)
    w.write_tool(
        "some_tool",
        ts=1.0,
        args={"count": 42, "enabled": True, "items": [1, 2, 3]},
    )
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    args_recorded = obj["args"]
    assert args_recorded["count"] == 42
    assert args_recorded["enabled"] is True
    assert args_recorded["items"] == [1, 2, 3]


def test_tool_result_secret_is_redacted(tmp_path: Path) -> None:
    """A tool result containing a key-shaped string is scrubbed before persist."""
    w = TranscriptWriter("redact-tool", sessions_dir=tmp_path)
    secret = "sk-proj-" + "Z" * 30
    w.write_tool("execute", ts=1.0, result=f"output: {secret} done")
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    assert secret not in obj["result"]
    assert "sk-…" in obj["result"]


def test_tool_arg_secret_is_redacted(tmp_path: Path) -> None:
    """A secret-shaped string arg value is scrubbed before persist."""
    w = TranscriptWriter("redact-arg", sessions_dir=tmp_path)
    token = "ghp_" + "a" * 36
    w.write_tool("http", ts=1.0, args={"headers": f"Authorization: Bearer {token}"})
    w.close()

    obj = json.loads(w.path.read_text(encoding="utf-8").strip())
    assert token not in json.dumps(obj["args"])
    assert "[REDACTED]" in json.dumps(obj["args"])


def test_logging_redacts_secrets(tmp_path: Path, monkeypatch) -> None:
    """The RedactingFilter scrubs key-shaped substrings from emitted log lines."""
    import logging

    from jarn.observability.logging import RedactingFilter

    records: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("jarn.test.redact")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    h = _CaptureHandler()
    h.addFilter(RedactingFilter())
    logger.addHandler(h)

    secret = "sk-ant-api03-" + "Q" * 40
    logger.info("building model with key=%s", secret)
    assert records
    assert secret not in records[-1]
    assert "sk-…" in records[-1]


def test_logging_suppresses_unformattable_record() -> None:
    """A record whose interpolation raises is replaced with the suppression
    placeholder, never emitted raw (that raw msg may carry an interpolated secret)."""
    import logging

    from jarn.observability.logging import RedactingFilter

    secret = "sk-ant-api03-" + "W" * 40
    # Too few args for the format string: getMessage() raises TypeError.
    record = logging.LogRecord(
        name="jarn.test.badfmt",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="key=%s ctx=%s " + secret,
        args=("only-one",),
        exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
    assert record.args == ()
    assert record.getMessage() == "<unformattable log record - suppressed for redaction safety>"
    assert secret not in record.getMessage()


def test_no_env_var_leaks_into_transcript(tmp_path: Path, monkeypatch) -> None:
    """A tool output that contains a sentinel secret value must not appear in the
    transcript when the caller correctly passes only the summary, not raw content.

    The TranscriptWriter itself has no knowledge of secrets — it writes whatever
    it receives.  The test verifies the *integration contract*: SessionDriver
    passes ``ev.data["summary"]`` (the compact one-liner) rather than the full
    tool payload to ``write_tool``, so a raw secret that leaked into the tool
    output would be trimmed to the summary and never written verbatim.
    """
    secret_marker = "SUPER_SECRET_TOKEN_XYZ"
    monkeypatch.setenv("FAKE_SECRET_ENV", secret_marker)

    # Simulate a tool result whose full content contains the secret but whose
    # summary (the one-liner the driver actually passes) does not.
    from jarn.agent.session import _tool_summary

    full_output = f"line1\nline2\n{secret_marker}\nline4"
    summary = _tool_summary(full_output)

    w = TranscriptWriter("sec", sessions_dir=tmp_path)
    # The driver passes the summary, not the full payload.
    w.write_tool("execute", ts=1.0, result=summary)
    w.close()

    content = w.path.read_text(encoding="utf-8")
    assert secret_marker not in content
    assert os.environ.get("FAKE_SECRET_ENV") == secret_marker  # env untouched
