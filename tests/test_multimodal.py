"""Multimodal file helpers + binary-aware diff + inline image content blocks."""

from __future__ import annotations

import asyncio
import base64
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from jarn.agent.files import is_multimodal_path, modality_of
from jarn.agent.session import Event, EventKind, SessionDriver
from jarn.config.schema import (
    Config,
    ExecutionConfig,
    PermissionMode,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
)
from jarn.cost import CostTracker
from jarn.permissions import PermissionEngine
from jarn.tui.widgets.diff import diff_from_edit_args

# A tiny but valid-enough PNG header + body; the scanner keys on the extension
# and the encoder base64s raw bytes, so exact PNG validity is irrelevant here.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"jarn-test-image-payload" * 4


def test_modality_detection():
    assert modality_of("a.png") == "image"
    assert modality_of("doc.pdf") == "document"
    assert modality_of("clip.mp4") == "video"
    assert modality_of("sound.mp3") == "audio"
    assert modality_of("main.py") == "text"


def test_is_multimodal():
    assert is_multimodal_path("photo.JPG")
    assert not is_multimodal_path("script.py")


def test_diff_skips_binary():
    diff = diff_from_edit_args({"file_path": "logo.png", "content": "<binary>"})
    assert "binary" in diff.plain


def test_diff_text_for_code():
    diff = diff_from_edit_args({"file_path": "a.py", "old_string": "x=1", "new_string": "x=2"})
    assert "x=1" in diff.plain and "x=2" in diff.plain


def test_large_write_diff_is_capped():
    """A big new file must not dump every line into the approval prompt."""
    content = "\n".join(f"line {i}" for i in range(500))
    diff = diff_from_edit_args(
        {"file_path": "big.py", "content": content}, max_lines=40
    )
    rendered = diff.plain.splitlines()
    # 40 capped lines + the "… (+N more lines)" footer
    assert len(rendered) <= 41
    assert "more lines" in diff.plain
    assert "line 0" in diff.plain        # head is shown
    assert "line 499" not in diff.plain  # tail is collapsed


def test_uncapped_diff_shows_everything():
    content = "\n".join(f"line {i}" for i in range(500))
    diff = diff_from_edit_args({"file_path": "big.py", "content": content})
    assert "line 499" in diff.plain
    assert "more lines" not in diff.plain


# ---------------------------------------------------------------------------
# T-3-7 — direct image content blocks
# ---------------------------------------------------------------------------


class _CaptureAgent:
    """Fake agent that records the payload handed to ``astream`` and streams nothing."""

    def __init__(self) -> None:
        self.payloads: list = []

    def astream(self, payload, config, *, stream_mode, subgraphs):
        self.payloads.append(payload)

        async def _gen():
            if False:  # pragma: no branch - makes _gen an async generator
                yield

        return _gen()


def _user_content(text, images):
    """Drive ``SessionDriver.run_turn`` with ``images`` and return the user
    message content the model would receive."""
    agent = _CaptureAgent()
    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t1",
    )

    async def _run():
        async for _ in driver.run_turn(text, images=images):
            pass

    asyncio.run(_run())
    return agent.payloads[0]["messages"][-1]["content"]


def _fake_controller(root, *, mode="auto"):
    cfg = Config(execution=ExecutionConfig(inline_images=mode))
    return SimpleNamespace(
        config=cfg, inline_images_disabled=False, project_root=root
    )


def test_inline_image_block(tmp_path):
    """A message mentioning an image (inline_images: auto) yields a user message
    whose content is [text-block, image-block] in the langchain-core shape."""
    from jarn.repl.turn import select_inline_images

    paste = tmp_path / ".jarn" / "pastes" / "paste-1.png"
    paste.parent.mkdir(parents=True)
    paste.write_bytes(_PNG_BYTES)

    text = "what is in @.jarn/pastes/paste-1.png"
    images = select_inline_images(_fake_controller(tmp_path), text)
    assert images == [paste]

    content = _user_content(text, images)
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": text}
    assert content[1]["type"] == "image"
    assert content[1]["mime_type"] == "image/png"
    assert base64.b64decode(content[1]["base64"]) == _PNG_BYTES


def test_size_cap_text_only(tmp_path):
    """A >5 MB image and a non-image mention both stay text-only (string content)."""
    from jarn.repl.turn import select_inline_images

    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * (5 * 1024 * 1024 + 1))
    note = tmp_path / "notes.txt"
    note.write_text("hello")

    text = "see @huge.png and @notes.txt"
    images = select_inline_images(_fake_controller(tmp_path), text)
    assert images == []

    content = _user_content(text, images)
    assert isinstance(content, str)
    assert content == text


def test_off_passthrough(tmp_path):
    """inline_images: off → no scanning, payload identical to today (string)."""
    from jarn.repl.turn import select_inline_images

    paste = tmp_path / "shot.png"
    paste.write_bytes(_PNG_BYTES)

    text = "look at @shot.png"
    images = select_inline_images(_fake_controller(tmp_path, mode="off"), text)
    assert images == []  # off → scanner is never consulted

    content = _user_content(text, None)
    assert isinstance(content, str)
    assert content == text
    # No scanning side effects: the file is untouched and still present.
    assert paste.read_bytes() == _PNG_BYTES


class _ImgFallbackDriver:
    """Fake driver recording resume/images and streaming a fixed event list."""

    def __init__(self, events) -> None:
        self._events = events
        self.resumed = None
        self.images = "UNSET"
        self.text = None

    async def run_turn(self, text, *, resume=False, images=None):
        self.text = text
        self.resumed = resume
        self.images = images
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_fallback_on_provider_error(tmp_path, monkeypatch):
    """A provider error carrying an image marker on a turn that inlined images
    triggers ONE same-model text-only retry + a NOTICE, and sets the session flag
    so later turns don't inline."""
    from jarn import repl
    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    ctrl = Controller(cfg, root)

    async def _noop_runtime():
        return None

    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)

    paste = root / "img.png"
    paste.write_bytes(_PNG_BYTES)

    first = _ImgFallbackDriver(
        [Event(EventKind.ERROR, "This model does not support image input",
               {"retryable": False})]
    )
    second = _ImgFallbackDriver(
        [Event(EventKind.TEXT, "text-only answer."), Event(EventKind.DONE)]
    )
    seq = [first, second]
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: seq.pop(0))

    async def _ask(_prompt):
        return ""

    console = Console(file=StringIO(), width=100)
    await repl._run_turn(console, ctrl, "describe @img.png", _ask, images=[paste])

    out = console.file.getvalue()
    assert "text-only answer." in out
    assert "image" in out.lower()          # a fallback NOTICE was emitted
    assert ctrl.inline_images_disabled is True
    assert first.images == [paste]         # first attempt inlined the image
    assert second.images is None           # retry was text-only
    assert second.resumed is False         # text is re-sent (not a bare resume)
    assert len(seq) == 0                    # exactly two attempts, no more
    ctrl.close()


# ── item E: drop_pending_image_message (the fallback crux) ──────────────────────


class _StateAgent:
    """Fake agent exposing aget_state/aupdate_state over a fixed message list."""

    def __init__(self, messages) -> None:
        self._messages = messages
        self.updates: list[dict] = []

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": self._messages})

    async def aupdate_state(self, config, update):
        self.updates.append(update)


def _img_ctrl(tmp_path, monkeypatch, messages):
    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    ctrl = Controller(cfg, root)
    agent = _StateAgent(messages)
    ctrl.runtime = SimpleNamespace(agent=agent)
    return ctrl, agent


@pytest.mark.asyncio
async def test_drop_pending_image_message_removes_image_human(tmp_path, monkeypatch):
    """A last HumanMessage carrying image blocks IS removed (RemoveMessage via
    aupdate_state) so the text-only re-send doesn't leave the rejected image in
    state."""
    messages = [
        SimpleNamespace(type="human", id="h1", content="earlier text turn"),
        SimpleNamespace(type="ai", id="a1", content="a reply"),
        SimpleNamespace(
            type="human",
            id="h2",
            content=[
                {"type": "text", "text": "describe this"},
                {"type": "image", "source_type": "base64", "data": "AAA="},
            ],
        ),
    ]
    ctrl, agent = _img_ctrl(tmp_path, monkeypatch, messages)

    removed = await ctrl.drop_pending_image_message()

    assert removed is True
    assert len(agent.updates) == 1, "the image-bearing human message must be removed"
    remove_msgs = agent.updates[0]["messages"]
    assert len(remove_msgs) == 1
    assert getattr(remove_msgs[0], "id", None) == "h2"
    ctrl.close()


@pytest.mark.asyncio
async def test_drop_pending_image_message_noop_without_image(tmp_path, monkeypatch):
    """A last HumanMessage with NO image blocks → returns without any update, so a
    prior text turn is never wrongly removed."""
    messages = [
        SimpleNamespace(type="human", id="h1", content="first"),
        SimpleNamespace(type="ai", id="a1", content="a reply"),
        SimpleNamespace(type="human", id="h2", content="plain text, no image"),
    ]
    ctrl, agent = _img_ctrl(tmp_path, monkeypatch, messages)

    removed = await ctrl.drop_pending_image_message()

    assert removed is False
    assert agent.updates == [], "must NOT remove a non-image human message"
    ctrl.close()
