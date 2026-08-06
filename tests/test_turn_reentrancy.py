"""T-QA-1: per-controller turn re-entrancy guard (refuse, never silent interleave)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jarn.agent.session import Event, EventKind
from jarn.agent.turn_runner import run_agent_turn
from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
from jarn.controller import Controller, TurnBusyError


class _BlockingDriver:
    """Driver that holds the turn open until ``release`` is set."""

    def __init__(self, release: asyncio.Event, started: asyncio.Event) -> None:
        self._release = release
        self._started = started
        self.run_count = 0

    async def run_turn(self, text, *, resume=False, images=None, **_kw):
        self.run_count += 1
        self._started.set()
        await self._release.wait()
        yield Event(EventKind.TEXT, "ok")
        yield Event(EventKind.DONE)


def _ctrl(tmp_path, monkeypatch) -> Controller:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={
            "openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")
        },
        routing=RoutingConfig(main="openrouter/m"),
    )
    return Controller(cfg, root)


@pytest.mark.asyncio
async def test_concurrent_run_agent_turn_refuses_second(tmp_path, monkeypatch):
    """Two concurrent run_agent_turns on one controller: second fails loud."""
    ctrl = _ctrl(tmp_path, monkeypatch)

    async def _noop():
        return None

    monkeypatch.setattr(ctrl, "ensure_runtime", _noop)

    release = asyncio.Event()
    started = asyncio.Event()
    driver = _BlockingDriver(release, started)
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: driver)

    t1 = asyncio.create_task(
        run_agent_turn(
            ctrl, "first", approver=lambda _r: None  # type: ignore[arg-type]
        )
    )
    await started.wait()
    assert ctrl._turn_held

    seen: list[Event] = []

    async def on_event(ev: Event) -> None:
        seen.append(ev)

    second = await run_agent_turn(
        ctrl,
        "second",
        approver=lambda _r: None,  # type: ignore[arg-type]
        on_event=on_event,
    )

    assert second.error is not None
    assert second.error.data.get("code") == "busy"
    assert "refusing concurrent" in (second.error.text or "").lower()
    assert len(seen) == 1 and seen[0].data.get("code") == "busy"
    # First driver must still be the only one that ran — no silent interleave.
    assert driver.run_count == 1

    release.set()
    first = await t1
    assert first.error is None
    assert driver.run_count == 1
    assert not ctrl._turn_held
    ctrl.close()


@pytest.mark.asyncio
async def test_concurrent_make_driver_refuses_and_preserves_active(
    tmp_path, monkeypatch
):
    """Second make_driver while a turn is held raises and does not overwrite."""
    ctrl = _ctrl(tmp_path, monkeypatch)
    ctrl.runtime = SimpleNamespace(
        agent=object(),
        main_model_ref="m",
        known_model_refs=(),
        progress_queue=None,
        backend=None,
    )

    first = ctrl.make_driver(lambda _r: None)  # type: ignore[arg-type]
    assert ctrl._turn_held
    assert ctrl._active_driver is first

    async def _other() -> None:
        with pytest.raises(TurnBusyError, match="refusing concurrent"):
            ctrl.make_driver(lambda _r: None)  # type: ignore[arg-type]

    await asyncio.create_task(_other())
    assert ctrl._active_driver is first
    ctrl.release_turn()
    ctrl.close()


@pytest.mark.asyncio
async def test_sequential_run_agent_turns_succeed(tmp_path, monkeypatch):
    """After the first turn releases, a second turn on the same thread is fine."""
    ctrl = _ctrl(tmp_path, monkeypatch)

    async def _noop():
        return None

    monkeypatch.setattr(ctrl, "ensure_runtime", _noop)

    class _Quick:
        async def run_turn(self, text, *, resume=False, images=None, **_kw):
            yield Event(EventKind.TEXT, text)
            yield Event(EventKind.DONE)

    monkeypatch.setattr(ctrl, "make_driver", lambda approver: _Quick())

    r1 = await run_agent_turn(
        ctrl, "a", approver=lambda _r: None  # type: ignore[arg-type]
    )
    r2 = await run_agent_turn(
        ctrl, "b", approver=lambda _r: None  # type: ignore[arg-type]
    )
    assert r1.error is None and r2.error is None
    assert not ctrl._turn_held
    ctrl.close()


@pytest.mark.asyncio
async def test_acquire_turn_steals_from_finished_owner(tmp_path, monkeypatch):
    """A done task's leaked hold must not wedge the controller forever."""
    ctrl = _ctrl(tmp_path, monkeypatch)

    async def _holder() -> None:
        assert ctrl.acquire_turn() is True
        # Deliberately do not release — simulate make_driver without run_turn.

    t = asyncio.create_task(_holder())
    await t
    assert t.done()
    # New task steals the slot.
    assert ctrl.acquire_turn() is True
    ctrl.release_turn()
    ctrl.close()
