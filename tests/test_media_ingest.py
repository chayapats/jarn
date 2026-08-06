"""Core multimodal ingest + document staging (#54 / T-MEDIA-1 + T-MEDIA-2)."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from jarn.agent.files import (
    INLINE_IMAGE_MAX_BYTES,
    image_content_block,
    image_content_block_from_bytes,
    image_mime_allowed,
)
from jarn.agent.media_ingest import (
    DOCUMENT_MAX_BYTES,
    MediaInput,
    cleanup_staging,
    expose_staging_roots,
    inject_document_paths,
    modality_from_mime,
    prepare_media,
    refuse_media,
    stage_document,
)
from jarn.agent.session import EventKind, SessionDriver, _build_user_content
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.permissions import PermissionEngine

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"jarn-test-image-payload" * 4


# ---------------------------------------------------------------------------
# files.py gates
# ---------------------------------------------------------------------------


def test_inline_image_max_bytes_is_5mb():
    assert INLINE_IMAGE_MAX_BYTES == 5 * 1024 * 1024


def test_image_mime_allowlist():
    assert image_mime_allowed("image/png")
    assert image_mime_allowed("image/jpeg")
    assert not image_mime_allowed("application/pdf")
    assert not image_mime_allowed("audio/ogg")
    assert not image_mime_allowed("image/heic")


def test_image_content_block_rejects_oversize(tmp_path):
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * (INLINE_IMAGE_MAX_BYTES + 1))
    assert image_content_block(big) is None


def test_image_content_block_rejects_non_image_mime(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    assert image_content_block(pdf) is None


def test_image_content_block_from_bytes_ok():
    block = image_content_block_from_bytes(_PNG_BYTES, "image/png")
    assert block is not None
    assert block["type"] == "image"
    assert block["mime_type"] == "image/png"
    assert base64.b64decode(block["base64"]) == _PNG_BYTES


def test_build_user_content_drops_gated_images(tmp_path):
    """Core gate on the legacy images= path — oversize never becomes a block."""
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * (INLINE_IMAGE_MAX_BYTES + 1))
    ok = tmp_path / "ok.png"
    ok.write_bytes(_PNG_BYTES)

    content = _build_user_content("hi", [big])
    assert content == "hi"

    content = _build_user_content("hi", [ok])
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "hi"}
    assert content[1]["type"] == "image"


# ---------------------------------------------------------------------------
# modality / refusal helpers
# ---------------------------------------------------------------------------


def test_modality_from_mime():
    assert modality_from_mime("image/png") == "image"
    assert modality_from_mime("application/pdf") == "document"
    assert modality_from_mime("text/plain") == "document"
    assert modality_from_mime("audio/ogg") == "audio"
    assert modality_from_mime("video/mp4") == "video"
    assert modality_from_mime("application/zip") == "unknown"


def test_refuse_voice_is_card_ready():
    refusal = refuse_media(
        reason="unsupported_modality",
        modality="audio",
        mime="audio/ogg",
        filename="note.oga",
    )
    assert refusal.reason == "unsupported_modality"
    assert "not supported" in refusal.message.lower()
    data = refusal.as_event_data()
    assert data["media_refusal"] is True
    assert data["modality"] == "audio"
    assert data["filename"] == "note.oga"
    assert data["message"] == refusal.message


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------


def test_stage_document_outside_project_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".jarn").mkdir()
    doc = stage_document(
        b"%PDF-1.4 hello",
        mime="application/pdf",
        filename="report.pdf",
        project_root=root,
    )
    assert doc.path.is_file()
    assert doc.path.read_bytes() == b"%PDF-1.4 hello"
    assert doc.filename == "report.pdf"
    # Must not land under the project or ~/.jarn.
    resolved = doc.staging_dir.resolve()
    with pytest.raises(ValueError):
        resolved.relative_to(root.resolve())
    with pytest.raises(ValueError):
        resolved.relative_to((Path.home() / ".jarn").resolve())
    cleanup_staging([doc.staging_dir])
    assert not doc.path.exists()


def test_stage_document_never_uses_jarn_run(tmp_path, monkeypatch):
    """Even if TMPDIR were poisoned, staging must not use ~/.jarn/run/."""
    root = tmp_path / "proj"
    root.mkdir()
    doc = stage_document(
        b"plain",
        mime="text/plain",
        filename="notes.txt",
        project_root=root,
    )
    parts = {p.lower() for p in doc.staging_dir.resolve().parts}
    assert ".jarn" not in parts
    cleanup_staging([doc.staging_dir])


def test_inject_document_paths_appends_absolute():
    from jarn.agent.media_ingest import StagedDocument

    staging = Path("/tmp/jarn-media-test")
    doc = StagedDocument(
        path=staging / "a.pdf",
        staging_dir=staging,
        mime="application/pdf",
        filename="a.pdf",
    )
    text = inject_document_paths("please review", [doc])
    assert text.startswith("please review\n\n")
    assert "application/pdf" in text
    assert "a.pdf" in text


def test_expose_staging_roots_undo():
    class _Backend:
        def __init__(self) -> None:
            self._extra_roots: list[Path] = []

    be = _Backend()
    d = Path("/tmp/jarn-stage-x").resolve()
    undo = expose_staging_roots(be, [d])
    assert d in be._extra_roots
    undo()
    assert be._extra_roots == []


# ---------------------------------------------------------------------------
# prepare_media
# ---------------------------------------------------------------------------


def test_prepare_image_bytes(tmp_path):
    prepared = prepare_media(
        "what is this?",
        [MediaInput(data=_PNG_BYTES, mime="image/png", filename="shot.png")],
        project_root=tmp_path,
    )
    assert prepared.refusals == []
    assert len(prepared.images) == 1
    assert prepared.images[0].read_bytes() == _PNG_BYTES
    assert prepared.text == "what is this?"
    prepared.cleanup()
    assert prepared.images == []


def test_prepare_image_path(tmp_path):
    img = tmp_path / "in.png"
    img.write_bytes(_PNG_BYTES)
    prepared = prepare_media(
        "look",
        [MediaInput(path=img, modality="image")],
        project_root=tmp_path,
    )
    assert prepared.images == [img]
    assert prepared.staging_dirs == []
    prepared.cleanup()


def test_prepare_oversize_image_refuses(tmp_path):
    prepared = prepare_media(
        "caption stays",
        [
            MediaInput(
                data=b"\x00" * (INLINE_IMAGE_MAX_BYTES + 1),
                mime="image/png",
                filename="huge.png",
            )
        ],
        project_root=tmp_path,
    )
    assert prepared.images == []
    assert prepared.text == "caption stays"
    assert len(prepared.refusals) == 1
    assert prepared.refusals[0].reason == "oversize"
    assert prepared.refusals[0].max_bytes == INLINE_IMAGE_MAX_BYTES


def test_prepare_document_stages_and_injects(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    prepared = prepare_media(
        "summarize this",
        [
            MediaInput(
                data=b"%PDF-1.4 body",
                mime="application/pdf",
                filename="spec.pdf",
                modality="document",
            )
        ],
        project_root=root,
    )
    assert prepared.refusals == []
    assert len(prepared.documents) == 1
    doc_path = prepared.documents[0].path
    assert doc_path.is_file()
    assert "spec.pdf" in prepared.text
    assert "summarize this" in prepared.text
    assert doc_path.resolve().as_posix() in prepared.text
    staging = list(prepared.staging_dirs)
    prepared.cleanup()
    assert not doc_path.exists()
    for d in staging:
        assert not d.exists()


def test_prepare_voice_refuses_but_caption_proceeds(tmp_path):
    prepared = prepare_media(
        "transcribe please",
        [
            MediaInput(
                data=b"OggSfake",
                mime="audio/ogg",
                filename="note.oga",
                modality="audio",
            )
        ],
        project_root=tmp_path,
    )
    assert prepared.images == []
    assert prepared.documents == []
    assert prepared.text == "transcribe please"
    assert len(prepared.refusals) == 1
    assert prepared.refusals[0].reason == "unsupported_modality"
    assert "voice" in prepared.refusals[0].message.lower() or "audio" in prepared.refusals[
        0
    ].message.lower()


def test_prepare_video_refuses(tmp_path):
    prepared = prepare_media(
        "",
        [MediaInput(data=b"\x00\x00", mime="video/mp4", filename="clip.mp4")],
        project_root=tmp_path,
    )
    assert prepared.refusals[0].modality == "video"
    assert prepared.text == ""


def test_prepare_disallowed_mime_refuses(tmp_path):
    prepared = prepare_media(
        "hi",
        [MediaInput(data=b"PK\x03\x04", mime="application/zip", filename="x.zip")],
        project_root=tmp_path,
    )
    assert prepared.refusals[0].reason in {"disallowed_type", "unsupported_modality"}


def test_prepare_text_document_allowlisted(tmp_path):
    prepared = prepare_media(
        "check",
        [MediaInput(data=b"hello\n", mime="text/plain", filename="a.txt")],
        project_root=tmp_path,
    )
    assert prepared.refusals == []
    assert len(prepared.documents) == 1
    prepared.cleanup()


def test_document_max_bytes_constant():
    assert DOCUMENT_MAX_BYTES == 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# SessionDriver.run_turn(media=…)
# ---------------------------------------------------------------------------


class _CaptureAgent:
    def __init__(self) -> None:
        self.payloads: list = []

    def astream(self, payload, config, *, stream_mode, subgraphs):
        self.payloads.append(payload)

        async def _gen():
            if False:  # pragma: no branch
                yield

        return _gen()


def test_run_turn_media_image_inlines(tmp_path):
    agent = _CaptureAgent()
    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="media-t1",
        project_root=tmp_path,
    )

    async def _run():
        events = []
        async for ev in driver.run_turn(
            "describe",
            media=[MediaInput(data=_PNG_BYTES, mime="image/png", filename="x.png")],
        ):
            events.append(ev)
        return events

    asyncio.run(_run())
    content = agent.payloads[0]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == "describe"
    assert content[1]["type"] == "image"


def test_run_turn_media_voice_notice_caption_proceeds(tmp_path):
    agent = _CaptureAgent()
    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="media-t2",
        project_root=tmp_path,
    )

    async def _run():
        events = []
        async for ev in driver.run_turn(
            "ignore the voice, answer this",
            media=[
                MediaInput(
                    data=b"OggS",
                    mime="audio/ogg",
                    filename="v.oga",
                    modality="voice",
                )
            ],
        ):
            events.append(ev)
        return events

    events = asyncio.run(_run())
    notices = [e for e in events if e.kind == EventKind.NOTICE]
    assert len(notices) == 1
    assert notices[0].data.get("media_refusal") is True
    content = agent.payloads[0]["messages"][-1]["content"]
    assert content == "ignore the voice, answer this"


def test_run_turn_media_document_injected_and_cleaned(tmp_path):
    class _Backend:
        def __init__(self) -> None:
            self._extra_roots: list[Path] = []
            self.seen_during_astream: list[Path] = []

    backend = _Backend()

    class _Agent(_CaptureAgent):
        def astream(self, payload, config, *, stream_mode, subgraphs):
            backend.seen_during_astream = list(backend._extra_roots)
            return super().astream(
                payload, config, stream_mode=stream_mode, subgraphs=subgraphs
            )

    agent = _Agent()
    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="media-t3",
        project_root=tmp_path,
        fs_backend=backend,
    )

    async def _run():
        async for _ in driver.run_turn(
            "read it",
            media=[
                MediaInput(
                    data=b"%PDF-1.4 x",
                    mime="application/pdf",
                    filename="d.pdf",
                    modality="document",
                )
            ],
        ):
            pass

    asyncio.run(_run())
    content = agent.payloads[0]["messages"][-1]["content"]
    assert isinstance(content, str)
    assert "d.pdf" in content
    assert "read it" in content
    # During the turn the staging dir was exposed; after, undone and deleted.
    assert backend.seen_during_astream
    assert backend._extra_roots == []
    for d in backend.seen_during_astream:
        assert not Path(d).exists()


def test_run_turn_media_only_refusal_no_model_call(tmp_path):
    agent = _CaptureAgent()
    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="media-t4",
        project_root=tmp_path,
    )

    async def _run():
        events = []
        async for ev in driver.run_turn(
            "",
            media=[MediaInput(data=b"x", mime="audio/ogg", filename="v.oga")],
        ):
            events.append(ev)
        return events

    events = asyncio.run(_run())
    assert any(e.kind == EventKind.NOTICE for e in events)
    assert any(e.kind == EventKind.DONE for e in events)
    assert agent.payloads == []


def test_tui_completion_shares_inline_cap():
    from jarn.tui.completion import INLINE_IMAGE_MAX_BYTES as tui_cap

    assert tui_cap is INLINE_IMAGE_MAX_BYTES
