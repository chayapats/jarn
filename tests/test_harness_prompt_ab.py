"""build_runtime's system_prompt_override — the seam the eval harness uses to
A/B J.A.R.N.'s harness prompt against a bare tool-using agent (same model/tools)."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.agent.builder import build_runtime
from jarn.config.schema import Config


def _build(tmp_path, override):
    cfg = Config()
    cfg.default_model = "openrouter/test-model"
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        return build_runtime(
            cfg, project_root=tmp_path, system_prompt_override=override
        )


def test_default_uses_jarn_persona(tmp_path):
    rt = _build(tmp_path, None)
    assert "Just A Reliable Nerd" in rt.system_prompt


def test_override_replaces_prompt_wholesale(tmp_path):
    rt = _build(tmp_path, "You are a coding assistant. Use the tools.")
    assert rt.system_prompt == "You are a coding assistant. Use the tools."
    assert "Reliable Nerd" not in rt.system_prompt


def test_empty_override_yields_empty_prompt(tmp_path):
    # "" is distinct from None: an explicit empty prompt (DeepAgents' own default
    # agent instructions still apply downstream — that's the "no harness" arm).
    rt = _build(tmp_path, "")
    assert rt.system_prompt == ""


def test_date_context_states_the_current_date():
    from datetime import datetime

    from jarn.agent.prompts import date_context

    block = date_context(datetime(2026, 6, 17, 9, 30, tzinfo=UTC))
    assert "2026-06-17" in block
    assert "Wednesday" in block
    assert "today" in block.lower()
    # Day granularity: no clock time in the stamp (else the per-day de-dup would
    # never match and every turn would re-inject the date).
    assert "09:30" not in block
    # DATE ONLY — no timezone abbreviation (a tz abbrev re-injected the date across
    # a DST transition on the same local calendar day).
    assert "UTC" not in block


def test_date_context_dst_boundary_same_local_day_is_single_block():
    """A DST transition (EST->EDT) on ONE local calendar day must not change the
    day stamp — else the per-day de-dup misses and the date double-injects."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from jarn.agent.prompts import date_context

    ny = ZoneInfo("America/New_York")
    # 2026-03-08: DST starts at 02:00. 01:30 is EST, 03:30 is EDT — same date.
    before = datetime(2026, 3, 8, 1, 30, tzinfo=ny)
    after = datetime(2026, 3, 8, 3, 30, tzinfo=ny)
    assert before.strftime("%Z") != after.strftime("%Z")  # tz abbrev really flips
    assert date_context(before) == date_context(after)


def test_jarn_prompt_injects_the_current_date(tmp_path):
    """The JARN system prompt tells the agent today's date, so time-sensitive
    requests ("find today's news") aren't anchored to the training cutoff."""
    rt = _build(tmp_path, None)
    assert "Current date:" in rt.system_prompt


def test_override_arm_has_no_date_injection(tmp_path):
    """The eval baseline (override) stays the pure controlled prompt — no date —
    so the A/B isolates the harness prompt."""
    rt = _build(tmp_path, "You are a coding assistant. Use the tools.")
    assert "Current date:" not in rt.system_prompt


@pytest.mark.asyncio
async def test_date_per_turn():
    """The controller builds a FRESH driver per turn but passes one shared
    ``date_state`` dict, so the date system message is injected once per local day
    (not every turn); a new local day re-injects it."""

    from jarn.agent.session import SessionDriver
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    class RecordingAgent:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def astream(self, payload, config, stream_mode=None, **kwargs):
            self.payloads.append(payload)
            yield ("messages", (type("Chunk", (), {"content": "ok"})(),))
            yield ("updates", {})

    # Session-lifetime dict shared across the per-turn drivers (as make_driver will).
    date_state: dict = {}

    def _driver(agent: RecordingAgent) -> SessionDriver:
        return SessionDriver(
            agent=agent,
            engine=PermissionEngine(),
            tracker=CostTracker(),
            thread_id="t-date",
            main_model_ref="test-model",
            date_state=date_state,
        )

    with patch("jarn.agent.session.date_context") as mock_date:
        # Turn 1 (day A, fresh driver): the date block is injected.
        mock_date.return_value = "Current date: Wednesday, 2026-06-17."
        agent1 = RecordingAgent()
        async for _ in _driver(agent1).run_turn("first"):
            pass
        msgs1 = agent1.payloads[0]["messages"]
        assert msgs1[0] == {
            "role": "system",
            "content": "Current date: Wednesday, 2026-06-17.",
        }
        assert msgs1[1] == {"role": "user", "content": "first"}

        # Turn 2 (SAME day, FRESH driver sharing date_state): NO duplicate date block.
        agent2 = RecordingAgent()
        async for _ in _driver(agent2).run_turn("second"):
            pass
        msgs2 = agent2.payloads[0]["messages"]
        assert msgs2[0] == {"role": "user", "content": "second"}

        # Turn 3 (NEW local day, fresh driver): the rolled-over date is re-injected.
        mock_date.return_value = "Current date: Thursday, 2026-06-18."
        agent3 = RecordingAgent()
        async for _ in _driver(agent3).run_turn("third"):
            pass
        msgs3 = agent3.payloads[0]["messages"]
        assert msgs3[0]["content"] == "Current date: Thursday, 2026-06-18."
        assert msgs3[1] == {"role": "user", "content": "third"}


@pytest.mark.asyncio
async def test_date_per_thread_new_thread_still_gets_its_date():
    """The de-dup is keyed by ``thread_id``: a new thread (after /clear, /compact,
    /rewind, /resume mints one) must get its OWN date system message even when
    another thread already stamped today with the SAME shared ``date_state``. A
    single shared "last" slot would suppress it, leaving the new thread dateless."""

    from jarn.agent.session import SessionDriver
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    class RecordingAgent:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def astream(self, payload, config, stream_mode=None, **kwargs):
            self.payloads.append(payload)
            yield ("messages", (type("Chunk", (), {"content": "ok"})(),))
            yield ("updates", {})

    date_state: dict = {}

    def _driver(agent: RecordingAgent, thread_id: str) -> SessionDriver:
        return SessionDriver(
            agent=agent,
            engine=PermissionEngine(),
            tracker=CostTracker(),
            thread_id=thread_id,
            main_model_ref="test-model",
            date_state=date_state,
        )

    with patch("jarn.agent.session.date_context") as mock_date:
        mock_date.return_value = "Current date: Wednesday, 2026-06-17."

        # Thread A gets its date block.
        agent_a = RecordingAgent()
        async for _ in _driver(agent_a, "thread-a").run_turn("hi A"):
            pass
        assert agent_a.payloads[0]["messages"][0]["content"].startswith("Current date:")

        # A SECOND turn on thread A (same day, same thread): no duplicate.
        agent_a2 = RecordingAgent()
        async for _ in _driver(agent_a2, "thread-a").run_turn("hi A again"):
            pass
        assert agent_a2.payloads[0]["messages"][0] == {"role": "user", "content": "hi A again"}

        # A DIFFERENT thread sharing the same date_state STILL gets its own date.
        agent_b = RecordingAgent()
        async for _ in _driver(agent_b, "thread-b").run_turn("hi B"):
            pass
        msgs_b = agent_b.payloads[0]["messages"]
        assert msgs_b[0] == {"role": "system", "content": "Current date: Wednesday, 2026-06-17."}
        assert msgs_b[1] == {"role": "user", "content": "hi B"}
