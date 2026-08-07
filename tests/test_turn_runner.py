"""Shared turn-runner policy (retry / fallback / T-3-7) — front-end agnostic."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarn.agent.session import Event, EventKind
from jarn.agent.turn_runner import (
    is_image_capability_error,
    run_agent_turn,
    select_inline_images,
)
from jarn.config.schema import (
    Config,
    ExecutionConfig,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
)


class _SeqDriver:
    """Fake driver recording resume/images and streaming a fixed event list."""

    def __init__(self, events) -> None:
        self._events = events
        self.resumed = None
        self.images = "UNSET"
        self.text = None

    async def run_turn(self, text, *, resume=False, images=None, **_kw):
        self.text = text
        self.resumed = resume
        self.images = images
        for e in self._events:
            yield e


def _ctrl(tmp_path, monkeypatch, *, fallback=None, inline="auto"):
    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={
            "openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")
        },
        routing=RoutingConfig(
            main="openrouter/m",
            fallback=list(fallback or []),
        ),
        execution=ExecutionConfig(inline_images=inline),
    )
    return Controller(cfg, root)


@pytest.mark.asyncio
async def test_retryable_error_rotates_fallback(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch, fallback=["openrouter/f1"])

    async def _noop():
        return None

    monkeypatch.setattr(ctrl, "ensure_runtime", _noop)

    first = _SeqDriver(
        [Event(EventKind.ERROR, "rate limit", {"retryable": True})]
    )
    second = _SeqDriver(
        [Event(EventKind.TEXT, "recovered"), Event(EventKind.DONE)]
    )
    seq = [first, second]
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: seq.pop(0))

    notices: list[str] = []

    async def on_event(ev: Event) -> None:
        if ev.kind is EventKind.NOTICE:
            notices.append(ev.text)

    result = await run_agent_turn(
        ctrl, "hi", approver=lambda _r: None, on_event=on_event  # type: ignore[arg-type]
    )

    assert result.error is None
    assert second.resumed is True
    assert any("retrying with openrouter/f1" in n for n in notices)
    assert len(seq) == 0
    ctrl.close()


@pytest.mark.asyncio
async def test_image_capability_text_only_retry(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch)
    paste = tmp_path / "proj" / "img.png"
    paste.write_bytes(b"\x89PNG\r\n" + b"x" * 32)

    async def _noop():
        return None

    monkeypatch.setattr(ctrl, "ensure_runtime", _noop)
    monkeypatch.setattr(ctrl, "drop_pending_image_message", _noop)

    first = _SeqDriver(
        [
            Event(
                EventKind.ERROR,
                "This model does not support image input",
                {"retryable": False},
            )
        ]
    )
    second = _SeqDriver(
        [Event(EventKind.TEXT, "ok"), Event(EventKind.DONE)]
    )
    seq = [first, second]
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: seq.pop(0))

    result = await run_agent_turn(
        ctrl,
        "describe",
        approver=lambda _r: None,  # type: ignore[arg-type]
        images=[paste],
    )

    assert result.error is None
    assert ctrl.inline_images_disabled is True
    assert first.images == [paste]
    assert second.images is None
    assert second.resumed is False
    ctrl.close()


@pytest.mark.asyncio
async def test_terminal_error_forwarded(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch)

    async def _noop():
        return None

    monkeypatch.setattr(ctrl, "ensure_runtime", _noop)
    monkeypatch.setattr(
        ctrl,
        "make_driver",
        lambda approver: _SeqDriver(
            [Event(EventKind.ERROR, "boom", {"retryable": False})]
        ),
    )

    seen: list[Event] = []

    async def on_event(ev: Event) -> None:
        seen.append(ev)

    result = await run_agent_turn(
        ctrl, "hi", approver=lambda _r: None, on_event=on_event  # type: ignore[arg-type]
    )

    assert result.error is not None
    assert result.error.text == "boom"
    assert len(seen) == 1 and seen[0].kind is EventKind.ERROR
    ctrl.close()


def test_select_inline_images_respects_off(tmp_path):
    root = tmp_path
    (root / "shot.png").write_bytes(b"\x89PNG\r\nxxxx")
    ctrl = SimpleNamespace(
        config=Config(execution=ExecutionConfig(inline_images="off")),
        inline_images_disabled=False,
        project_root=root,
    )
    assert select_inline_images(ctrl, "see @shot.png") == []


def test_is_image_capability_error_markers():
    assert is_image_capability_error(
        Event(EventKind.ERROR, "no vision support here")
    )
    assert not is_image_capability_error(
        Event(EventKind.ERROR, "rate limit exceeded")
    )
