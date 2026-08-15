"""Shared ``session_label()`` for ``/sessions`` and the ``/resume`` picker."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from jarn.controller.commands.session import cmd_sessions
from jarn.memory.sessions import SessionInfo, session_label
from jarn.repl.commands import CommandMixin
from jarn.tui import layout


def test_session_label_session_info_uses_eight_char_id() -> None:
    session = SessionInfo(
        thread_id="abcdefghij",
        title="Fix toolbar",
        updated_at=0.0,
        state="complete",
    )
    assert session_label(session) == f"{session.updated_human}  Fix toolbar  abcdefgh"
    assert session_label(session).endswith("  abcdefgh")
    assert "abcdefghij" not in session_label(session)


def test_session_label_empty_title_is_untitled() -> None:
    session = SessionInfo(thread_id="abcdefghij", title="", updated_at=0.0)
    assert session_label(session) == f"{session.updated_human}  (untitled)  abcdefgh"


def test_session_label_missing_title_on_fake_is_untitled() -> None:
    fake = SimpleNamespace(updated_human="now", thread_id="thread-parked")
    assert session_label(fake) == "now  (untitled)  thread-p"


def test_session_label_accepts_resume_picker_simple_namespace() -> None:
    fake = SimpleNamespace(
        updated_human="now", title="parked work", thread_id="thread-parked"
    )
    assert session_label(fake) == "now  parked work  thread-p"


def test_resume_picker_format_equals_session_label() -> None:
    """Picker copy is the helper so ``/sessions`` and ``/resume`` cannot drift."""
    session = SessionInfo(
        thread_id="abcdefghij",
        title="Fix toolbar",
        updated_at=0.0,
    )
    assert session_label(session) == (
        f"{session.updated_human}  {session.title}  {session.thread_id[:8]}"
    )
    source = inspect.getsource(CommandMixin._resume_picker)
    assert "session_label(s)" in source
    assert "s.updated_human" not in source


def test_cmd_sessions_uses_session_label_and_current_marker() -> None:
    session = SessionInfo(
        thread_id="abcdefghij",
        title="Fix toolbar",
        updated_at=0.0,
        state="complete",
        project_root="/tmp/proj",
        model="gpt-4",
    )
    ctrl = SimpleNamespace(
        sessions=SimpleNamespace(list=lambda: [session]),
        thread_id="abcdefghij",
    )
    text = cmd_sessions(ctrl, "").text
    assert layout.escape(session_label(session)) in text
    assert "Fix toolbar" in text
    assert "abcdefgh" in text
    assert "→" in text
    assert "complete" in text
    assert "/tmp/proj" in text
    assert "gpt-4" in text


def test_cmd_sessions_filters_by_query() -> None:
    toolbar = SessionInfo(
        thread_id="aaaaaaaa",
        title="Fix toolbar",
        updated_at=0.0,
        state="complete",
    )
    other = SessionInfo(
        thread_id="bbbbbbbb",
        title="Unrelated work",
        updated_at=0.0,
        state="complete",
    )
    ctrl = SimpleNamespace(
        sessions=SimpleNamespace(list=lambda: [toolbar, other]),
        thread_id="aaaaaaaa",
    )
    text = cmd_sessions(ctrl, "toolbar").text
    assert "Fix toolbar" in text
    assert "Unrelated work" not in text
    prefix = cmd_sessions(ctrl, "bbbb").text
    assert "Unrelated work" in prefix
    assert "Fix toolbar" not in prefix
    empty = cmd_sessions(ctrl, "nope").text
    assert "No sessions matching" in empty


def test_cmd_sessions_escapes_markup_in_title_once() -> None:
    session = SessionInfo(
        thread_id="abcdefghij",
        title="[bold]oops",
        updated_at=0.0,
        state="incomplete",
    )
    ctrl = SimpleNamespace(
        sessions=SimpleNamespace(list=lambda: [session]),
        thread_id="other-thread",
    )
    text = cmd_sessions(ctrl, "").text
    escaped = layout.escape(session_label(session))
    assert escaped in text
    assert text.count(escaped) == 1
    assert "interrupted" in text
    assert "[bold]oops" not in text.replace(escaped, "")
