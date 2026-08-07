"""In-gateway job store + catch-up-once tick (T-SCHED-1 / #42)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jarn.gateway.scheduler import (
    JOBS_FILENAME,
    CronExpr,
    DueWork,
    ScheduleError,
    Scheduler,
    clear_active_delivery,
    get_active_delivery,
    jobs_path,
    next_cron_after,
    schedule_tool_call,
    set_active_delivery,
)


@pytest.fixture
def personal(isolated_home: Path) -> Path:
    """Personal root with a fake ``.git`` (avoid ``git init`` in the sandbox)."""
    root = isolated_home / "personal"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    return root.resolve()


def test_jobs_path_under_jarn_home(isolated_home: Path) -> None:
    assert jobs_path() == isolated_home / "gateway" / JOBS_FILENAME


def test_add_list_remove_enable(isolated_home: Path, personal: Path) -> None:
    sched = Scheduler()
    now = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)

    job = sched.add(
        prompt="morning standup",
        chat_id=42,
        cron="0 9 * * *",
        now=now,
    )
    assert job.chat_id == 42
    assert job.root == str(personal)
    assert job.enabled is True
    assert job.next_run == "2026-08-08T09:00:00Z"

    listed = sched.list_jobs()
    assert len(listed) == 1
    assert listed[0].id == job.id

    disabled = sched.enable(job.id, False)
    assert disabled.enabled is False

    enabled = sched.enable(job.id, True, now=now)
    assert enabled.enabled is True
    assert enabled.next_run == "2026-08-08T09:00:00Z"

    removed = sched.remove(job.id)
    assert removed is not None
    assert sched.list_jobs() == []


def test_one_shot_at(isolated_home: Path, personal: Path) -> None:
    sched = Scheduler()
    now = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)
    job = sched.add(
        prompt="remind me",
        chat_id=7,
        at="2026-08-07T15:30:00Z",
        now=now,
    )
    assert job.cron is None
    assert job.at == "2026-08-07T15:30:00Z"
    assert job.next_run == "2026-08-07T15:30:00Z"

    with pytest.raises(ScheduleError, match="not in the future"):
        sched.add(prompt="late", chat_id=7, at="2026-08-07T09:00:00Z", now=now)


def test_catch_up_once_not_storm(isolated_home: Path, personal: Path) -> None:
    """After downtime spanning many cron ticks, run_due fires once then jumps ahead."""
    sched = Scheduler()
    created = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    job = sched.add(
        prompt="hourly",
        chat_id=1,
        cron="0 * * * *",
        now=created,
        job_id="hourly1",
    )
    assert job.next_run == "2026-08-01T01:00:00Z"

    now = datetime(2026, 8, 7, 12, 30, tzinfo=UTC)
    due = sched.run_due(now=now)
    assert len(due) == 1
    assert due[0].job_id == "hourly1"
    assert due[0].prompt == "hourly"
    assert due[0].chat_id == 1

    refreshed = sched.get("hourly1")
    assert refreshed is not None
    assert refreshed.last_run == "2026-08-07T12:30:00Z"
    assert refreshed.next_run == "2026-08-07T13:00:00Z"
    assert sched.tick(now=now) == []


def test_one_shot_disables_after_catch_up(isolated_home: Path, personal: Path) -> None:
    sched = Scheduler()
    now = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)
    sched.add(
        prompt="once",
        chat_id=3,
        at="2026-08-07T09:00:00Z",
        now=datetime(2026, 8, 7, 8, 0, tzinfo=UTC),
        job_id="once1",
    )
    due = sched.run_due(now=now)
    assert len(due) == 1
    job = sched.get("once1")
    assert job is not None
    assert job.enabled is False
    assert job.next_run is None
    assert sched.tick(now=now + timedelta(hours=1)) == []


def test_cron_next_and_match() -> None:
    expr = CronExpr.parse("30 14 * * 1")
    hit = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
    assert expr.matches(hit)
    assert not expr.matches(hit.replace(minute=31))
    nxt = next_cron_after(expr, datetime(2026, 8, 7, 0, 0, tzinfo=UTC))
    assert nxt == hit


def test_active_delivery_roundtrip(isolated_home: Path, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    set_active_delivery(chat_id=99, root=root, thread_id="th-1")
    got = get_active_delivery(root)
    assert got is not None
    assert got.chat_id == 99
    assert got.thread_id == "th-1"
    clear_active_delivery(root)
    assert get_active_delivery(root) is None


def test_schedule_tool_create_list_remove(isolated_home: Path, personal: Path) -> None:
    created = schedule_tool_call(
        action="propose",
        prompt="check inbox",
        at="2099-01-01T12:00:00Z",
        chat_id=55,
    )
    assert "Scheduled job" in created
    assert "check inbox" in created

    listed = schedule_tool_call(action="list", chat_id=55)
    assert "check inbox" in listed
    assert "55" in listed

    jobs = Scheduler().list_jobs(chat_id=55)
    assert len(jobs) == 1
    jid = jobs[0].id

    removed = schedule_tool_call(action="remove", job_id=jid)
    assert "Removed" in removed
    assert Scheduler().list_jobs(chat_id=55) == []


def test_schedule_tool_inherits_active_delivery(
    isolated_home: Path, personal: Path
) -> None:
    set_active_delivery(chat_id=1234, root=personal)
    out = schedule_tool_call(
        action="create",
        prompt="from context",
        at="2099-06-01T00:00:00Z",
        default_root=personal,
    )
    assert "chat_id=1234" in out
    job = Scheduler().list_jobs(chat_id=1234)[0]
    assert job.prompt == "from context"


def test_dispatch_due_calls_submit(isolated_home: Path, personal: Path) -> None:
    sched = Scheduler()
    sched.add(
        prompt="go",
        chat_id=8,
        at="2026-08-07T09:00:00Z",
        now=datetime(2026, 8, 7, 8, 0, tzinfo=UTC),
        job_id="d1",
    )
    seen: list[DueWork] = []
    due = sched.dispatch_due(seen.append, now=datetime(2026, 8, 7, 10, 0, tzinfo=UTC))
    assert len(due) == 1
    assert seen[0].job_id == "d1"
    assert seen[0].prompt == "go"


def test_session_router_tick_scheduler(isolated_home: Path, personal: Path) -> None:
    from jarn.gateway.daemon import DaemonSupervisor
    from jarn.gateway.sessions import SessionRouter

    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.get_worker.return_value = None
    supervisor.on_outbound = None
    supervisor.on_worker_death = None

    sent: list[tuple] = []

    def fake_send(root, frame):
        sent.append((root, frame))

    supervisor.send.side_effect = fake_send

    router = SessionRouter(supervisor, personal_root=personal)
    sched = Scheduler()
    sched.add(
        prompt="scheduled ping",
        chat_id=11,
        at="2026-08-07T09:00:00Z",
        now=datetime(2026, 8, 7, 8, 0, tzinfo=UTC),
        root=personal,
        job_id="s1",
    )
    due = router.tick_scheduler(sched, now=datetime(2026, 8, 7, 10, 0, tzinfo=UTC))
    assert len(due) == 1
    assert len(sent) == 1
    assert sent[0][1].text == "scheduled ping"
    hint = get_active_delivery(personal)
    assert hint is not None
    assert hint.chat_id == 11
