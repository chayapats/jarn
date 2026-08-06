"""T-SEC-1: fail-closed redaction of TOOL_START args before they leave on the wire.

Measured failure modes this covers:

1. A huge ``write_file`` content arg used to ride out uncapped on TOOL_START —
   emitted from the updates stream *before* HITL interrupt resolution, so a
   DENY could not take the payload back.
2. A ``.env`` write carrying ``NAME=secret`` / vendor-key values used to publish
   those secrets on the same TOOL_START event, again before the denied path ran.
"""

from __future__ import annotations

import json

from jarn.agent.events import EventKind
from jarn.agent.session import SessionDriver
from jarn.agent.stream_handlers import handle_update_chunk
from jarn.agent.tool_arg_redact import (
    _MAX_ARG_CHARS,
    _MAX_ARG_TOTAL_CHARS,
    sanitize_tool_args,
)
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.permissions import PermissionEngine


def _driver() -> SessionDriver:
    return SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="sec-redact",
    )


def _tool_start_for(name: str, args: dict) -> object:
    ai = type(
        "AIMessage",
        (),
        {"tool_calls": [{"id": "c1", "name": name, "args": args}]},
    )()
    chunk = {"model": {"messages": [ai]}}
    events = list(handle_update_chunk(_driver(), chunk, interrupts=[]))
    assert len(events) == 1
    assert events[0].kind is EventKind.TOOL_START
    return events[0]


def test_sanitize_tool_args_caps_huge_write_file_content():
    big = "x" * (_MAX_ARG_CHARS + 5_000)
    out = sanitize_tool_args({"file_path": ".env", "content": big})
    assert len(out["content"]) == _MAX_ARG_CHARS
    assert out.get("content__truncated") is True
    assert out["file_path"] == ".env"


def test_sanitize_tool_args_redacts_dotenv_secret_values():
    content = (
        "OPENAI_API_KEY=sk-proj-ABCDEFGH1234567890WXYZ\n"
        "DATABASE_PASSWORD=hunter2-super-secret\n"
        "GITHUB_TOKEN=ghp_" + ("a" * 36) + "\n"
    )
    out = sanitize_tool_args({"file_path": ".env", "content": content})
    wire = json.dumps(out)
    assert "sk-proj-ABCDEFGH1234567890WXYZ" not in wire
    assert "hunter2-super-secret" not in wire
    assert "ghp_" + ("a" * 36) not in wire
    assert "[REDACTED]" in out["content"] or "sk-…" in out["content"]


def test_tool_start_event_caps_huge_write_file_args():
    """Regression: TOOL_START must not publish uncapped write_file content."""
    big = "y" * (_MAX_ARG_TOTAL_CHARS + 50_000)
    ev = _tool_start_for(
        "write_file",
        {"file_path": "blob.bin", "content": big},
    )
    args = ev.data["args"]
    wire = json.dumps(args)
    assert len(args["content"]) <= _MAX_ARG_CHARS
    assert args.get("content__truncated") is True
    assert len(wire) < _MAX_ARG_TOTAL_CHARS * 2


def test_tool_start_event_redacts_dotenv_secrets_before_wire():
    """Regression: TOOL_START must not publish .env secret values on the wire.

    Ordering matter: this event is emitted from the updates stream *before*
    interrupt/approval resolution, so a later DENY cannot unsay a leak — the
    payload must already be redacted at Event construction.
    """
    secret = "sk-proj-LEAKME1234567890ABCDEF"
    password = "dotenv-password-value-9f8a7b"
    ghp = "ghp_" + ("b" * 36)
    ev = _tool_start_for(
        "write_file",
        {
            "file_path": ".env",
            "content": (
                f"OPENAI_API_KEY={secret}\n"
                f"DATABASE_PASSWORD={password}\n"
                f"GH_TOKEN={ghp}\n"
            ),
        },
    )
    wire = json.dumps(ev.data)
    assert secret not in wire
    assert password not in wire
    assert ghp not in wire


def test_tool_start_redaction_preserves_internal_edit_target():
    """Bookkeeping reads raw args before sanitization — path must still land."""
    driver = _driver()
    ai = type(
        "AIMessage",
        (),
        {
            "tool_calls": [
                {
                    "id": "c2",
                    "name": "write_file",
                    "args": {
                        "file_path": "secrets/.env",
                        "content": "API_KEY=should-not-leak-abcdef0123456789",
                    },
                }
            ]
        },
    )()
    events = list(
        handle_update_chunk(driver, {"model": {"messages": [ai]}}, interrupts=[])
    )
    assert driver._last_edit_target == "secrets/.env"
    assert "should-not-leak-abcdef0123456789" not in json.dumps(events[0].data)


def test_denied_path_cannot_rely_on_raw_tool_start_args():
    """Even when an interrupt is collected in the same turn, TOOL_START already
    carries only the sanitized copy — a DENY cannot be what first redacts."""
    driver = _driver()
    secret = "sk-proj-DENIEDPATH1234567890XY"
    ai = type(
        "AIMessage",
        (),
        {
            "tool_calls": [
                {
                    "id": "c3",
                    "name": "write_file",
                    "args": {"file_path": ".env", "content": f"API_KEY={secret}"},
                }
            ]
        },
    )()
    interrupts: list = []
    # First the model update (TOOL_START) — this is the leak window.
    starts = list(
        handle_update_chunk(driver, {"model": {"messages": [ai]}}, interrupts)
    )
    # Then the interrupt arrives (deny will be resolved later by the driver).
    # Consume the generator — interrupt collection runs on iteration.
    list(
        handle_update_chunk(
            driver,
            {
                "__interrupt__": [
                    type(
                        "Intr",
                        (),
                        {
                            "value": {
                                "action_requests": [
                                    {
                                        "name": "write_file",
                                        "args": {
                                            "file_path": ".env",
                                            "content": f"API_KEY={secret}",
                                        },
                                    }
                                ]
                            }
                        },
                    )()
                ]
            },
            interrupts,
        )
    )
    assert len(starts) == 1
    assert starts[0].kind is EventKind.TOOL_START
    assert secret not in json.dumps(starts[0].data)
    assert len(interrupts) == 1  # deny path still has the interrupt to resolve
