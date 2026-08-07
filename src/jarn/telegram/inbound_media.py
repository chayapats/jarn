"""Inbound Telegram media → core ``prepare_media`` → ``MediaRef`` (#54 / T-TG-4).

Download via Bot API, gate/stage through :mod:`jarn.agent.media_ingest`, and
produce media refs for worker ``turn`` frames. Voice / unsupported modalities
yield refusal cards; caption/text may still proceed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jarn.agent.media_ingest import (
    MediaInput,
    MediaRefusal,
    PreparedMedia,
    prepare_media,
)
from jarn.gateway.protocol import MediaRef

_log = logging.getLogger("jarn.telegram.inbound_media")

__all__ = [
    "InboundMediaResult",
    "TelegramFileDownloader",
    "collect_message_media",
    "download_and_prepare",
    "media_refs_from_prepared",
    "modality_hint_for_telegram",
]


class TelegramFileDownloader(Protocol):
    """Minimal surface: resolve a ``file_id`` to bytes (aiogram Bot or fake)."""

    async def download_file_bytes(self, file_id: str) -> bytes: ...

    async def get_file_path(self, file_id: str) -> str | None:
        """Optional Bot API path hint (may be unused by fakes)."""
        ...


@dataclass(slots=True)
class InboundMediaResult:
    """Outcome of downloading + gating one Telegram message's attachments."""

    text: str
    media_refs: list[MediaRef] = field(default_factory=list)
    refusals: list[MediaRefusal] = field(default_factory=list)
    prepared: PreparedMedia | None = None

    @property
    def has_work(self) -> bool:
        return bool(self.text.strip() or self.media_refs)


def modality_hint_for_telegram(attr: str) -> str:
    """Map a Telegram message attachment attribute to a core modality hint."""
    mapping = {
        "photo": "image",
        "document": "document",
        "voice": "voice",
        "audio": "audio",
        "video": "video",
        "video_note": "video",
        "animation": "video",
        "sticker": "unknown",
    }
    return mapping.get(attr, "unknown")


def collect_message_media(message: Any) -> list[tuple[str, str, str | None, str]]:
    """Extract ``(file_id, modality, mime, filename)`` tuples from a message.

    Does not download. Multiple photo sizes → largest only.
    """
    items: list[tuple[str, str, str | None, str]] = []
    if message is None:
        return items

    photos = getattr(message, "photo", None)
    if photos is None and isinstance(message, dict):
        photos = message.get("photo")
    if photos:
        # Telegram sends sizes ascending; take the last (largest).
        largest = photos[-1]
        file_id = getattr(largest, "file_id", None)
        if file_id is None and isinstance(largest, dict):
            file_id = largest.get("file_id")
        if file_id:
            items.append((str(file_id), "image", "image/jpeg", "photo.jpg"))

    for attr, default_mime in (
        ("document", None),
        ("voice", "audio/ogg"),
        ("audio", None),
        ("video", None),
        ("video_note", "video/mp4"),
        ("animation", None),
        ("sticker", None),
    ):
        obj = getattr(message, attr, None)
        if obj is None and isinstance(message, dict):
            obj = message.get(attr)
        if obj is None:
            continue
        file_id = getattr(obj, "file_id", None)
        if file_id is None and isinstance(obj, dict):
            file_id = obj.get("file_id")
        if not file_id:
            continue
        mime = getattr(obj, "mime_type", None)
        if mime is None and isinstance(obj, dict):
            mime = obj.get("mime_type")
        mime = mime or default_mime
        filename = getattr(obj, "file_name", None)
        if filename is None and isinstance(obj, dict):
            filename = obj.get("file_name")
        if filename is None:
            filename = getattr(obj, "file_unique_id", None) or attr
        modality = modality_hint_for_telegram(attr)
        items.append((str(file_id), modality, mime, str(filename)))

    return items


async def download_and_prepare(
    message: Any,
    downloader: TelegramFileDownloader,
    *,
    caption: str = "",
    project_root: Path | None = None,
) -> InboundMediaResult:
    """Download attachments, run :func:`prepare_media`, return refs + refusals.

    Unsupported modalities (voice/audio/video) are refused **without** download
    when the modality hint is already known unsupported — saves bandwidth and
    matches #54 ("do not download/stage into the model path").
    """
    from jarn.agent.media_ingest import UNSUPPORTED_MODALITIES

    text = caption or _message_text(message) or ""
    specs = collect_message_media(message)
    if not specs:
        return InboundMediaResult(text=text)

    inputs: list[MediaInput] = []
    early_refusals: list[MediaRefusal] = []

    for file_id, modality, mime, filename in specs:
        if modality in UNSUPPORTED_MODALITIES:
            from jarn.agent.media_ingest import refuse_media

            early_refusals.append(
                refuse_media(
                    reason="unsupported_modality",
                    modality=modality,
                    mime=mime or "",
                    filename=filename,
                )
            )
            continue
        try:
            data = await downloader.download_file_bytes(file_id)
        except Exception as exc:  # noqa: BLE001 — surface as refusal, don't brick
            _log.warning("telegram media download failed file_id=%s: %s", file_id, exc)
            from jarn.agent.media_ingest import refuse_media

            early_refusals.append(
                refuse_media(
                    reason="unreadable",
                    modality=modality,
                    mime=mime or "",
                    filename=filename,
                )
            )
            continue
        inputs.append(
            MediaInput(
                data=data,
                mime=mime,
                modality=modality,
                filename=filename,
            )
        )

    prepared = prepare_media(text, inputs, project_root=project_root)
    prepared.refusals = list(early_refusals) + list(prepared.refusals)
    refs = media_refs_from_prepared(prepared)
    return InboundMediaResult(
        text=prepared.text,
        media_refs=refs,
        refusals=list(prepared.refusals),
        prepared=prepared,
    )


def media_refs_from_prepared(prepared: PreparedMedia) -> list[MediaRef]:
    """Convert core prepared images/documents into worker ``MediaRef`` rows."""
    refs: list[MediaRef] = []
    for path in prepared.images:
        mime = _guess_mime(path) or "image/jpeg"
        refs.append(MediaRef(path=str(path.resolve()), mime=mime, modality="image"))
    for doc in prepared.documents:
        refs.append(
            MediaRef(
                path=str(doc.path.resolve()),
                mime=doc.mime,
                modality="document",
            )
        )
    return refs


def _message_text(message: Any) -> str:
    if message is None:
        return ""
    for attr in ("text", "caption"):
        value = getattr(message, attr, None)
        if value is None and isinstance(message, dict):
            value = message.get(attr)
        if isinstance(value, str) and value:
            return value
    return ""


def _guess_mime(path: Path) -> str | None:
    import mimetypes

    mime, _ = mimetypes.guess_type(str(path))
    return mime


@dataclass
class AiogramDownloader:
    """Adapter: aiogram ``Bot`` → :class:`TelegramFileDownloader`."""

    bot: Any

    async def download_file_bytes(self, file_id: str) -> bytes:
        file = await self.bot.get_file(file_id)
        buf = await self.bot.download_file(file.file_path)
        if hasattr(buf, "read"):
            data = buf.read()
            if hasattr(data, "__await__"):
                data = await data
            return bytes(data)
        if isinstance(buf, (bytes, bytearray)):
            return bytes(buf)
        # aiogram may return a path-like / BytesIO
        return bytes(buf.getvalue()) if hasattr(buf, "getvalue") else bytes(buf)

    async def get_file_path(self, file_id: str) -> str | None:
        file = await self.bot.get_file(file_id)
        return getattr(file, "file_path", None)
