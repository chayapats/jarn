"""T-TG-4: inbound media download → prepare_media → MediaRef (#54)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from jarn.agent.media_ingest import INLINE_IMAGE_MAX_BYTES
from jarn.telegram.inbound_media import (
    collect_message_media,
    download_and_prepare,
    media_refs_from_prepared,
    modality_hint_for_telegram,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"jarn-telegram-media" * 8


@dataclass
class FakeDownloader:
    files: dict[str, bytes] = field(default_factory=dict)
    downloaded: list[str] = field(default_factory=list)

    async def download_file_bytes(self, file_id: str) -> bytes:
        self.downloaded.append(file_id)
        if file_id not in self.files:
            raise FileNotFoundError(file_id)
        return self.files[file_id]

    async def get_file_path(self, file_id: str) -> str | None:
        return f"/tmp/{file_id}"


def test_modality_hints():
    assert modality_hint_for_telegram("photo") == "image"
    assert modality_hint_for_telegram("voice") == "voice"
    assert modality_hint_for_telegram("document") == "document"


def test_collect_largest_photo_and_document():
    msg = SimpleNamespace(
        photo=[
            SimpleNamespace(file_id="small", file_size=10),
            SimpleNamespace(file_id="large", file_size=99),
        ],
        document=SimpleNamespace(
            file_id="doc1", mime_type="application/pdf", file_name="a.pdf"
        ),
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        animation=None,
        sticker=None,
        text=None,
        caption="see this",
    )
    items = collect_message_media(msg)
    assert items[0][0] == "large"
    assert items[0][1] == "image"
    assert any(i[0] == "doc1" and i[1] == "document" for i in items)


@pytest.mark.asyncio
async def test_voice_refused_without_download(tmp_path):
    dl = FakeDownloader(files={"v1": b"ogg-bytes"})
    msg = SimpleNamespace(
        photo=None,
        document=None,
        voice=SimpleNamespace(file_id="v1", mime_type="audio/ogg"),
        audio=None,
        video=None,
        video_note=None,
        animation=None,
        sticker=None,
        text=None,
        caption="transcribe please",
    )
    result = await download_and_prepare(
        msg, dl, caption="transcribe please", project_root=tmp_path
    )
    assert dl.downloaded == []  # must not download voice
    assert result.refusals
    assert result.refusals[0].modality in {"voice", "audio"}
    assert "transcribe please" in result.text
    assert result.media_refs == []


@pytest.mark.asyncio
async def test_photo_becomes_media_ref(tmp_path):
    dl = FakeDownloader(files={"p1": _PNG})
    msg = SimpleNamespace(
        photo=[SimpleNamespace(file_id="p1")],
        document=None,
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        animation=None,
        sticker=None,
        text=None,
        caption="look",
    )
    result = await download_and_prepare(
        msg, dl, caption="look", project_root=tmp_path
    )
    assert dl.downloaded == ["p1"]
    assert result.refusals == []
    assert len(result.media_refs) == 1
    assert result.media_refs[0].modality == "image"
    assert result.text == "look"
    refs = media_refs_from_prepared(result.prepared)
    assert refs[0].path == result.media_refs[0].path


@pytest.mark.asyncio
async def test_pdf_document_staged_outside_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    pdf = b"%PDF-1.4 jarn-test"
    dl = FakeDownloader(files={"d1": pdf})
    msg = SimpleNamespace(
        photo=None,
        document=SimpleNamespace(
            file_id="d1", mime_type="application/pdf", file_name="notes.pdf"
        ),
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        animation=None,
        sticker=None,
        text=None,
        caption="read this",
    )
    result = await download_and_prepare(
        msg, dl, caption="read this", project_root=root
    )
    assert result.media_refs
    assert result.media_refs[0].modality == "document"
    staged = result.media_refs[0].path
    assert staged.startswith(str(root)) is False
    assert "notes.pdf" in result.text or staged in result.text


@pytest.mark.asyncio
async def test_oversize_image_refusal(tmp_path):
    big = b"\x89PNG\r\n\x1a\n" + b"x" * (INLINE_IMAGE_MAX_BYTES + 10)
    dl = FakeDownloader(files={"big": big})
    msg = SimpleNamespace(
        photo=[SimpleNamespace(file_id="big")],
        document=None,
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        animation=None,
        sticker=None,
        text=None,
        caption="huge",
    )
    result = await download_and_prepare(msg, dl, caption="huge", project_root=tmp_path)
    assert result.refusals
    assert result.refusals[0].reason == "oversize"
    assert result.media_refs == []
    assert "huge" in result.text
