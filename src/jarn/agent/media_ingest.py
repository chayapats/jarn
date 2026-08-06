"""Core multimodal ingest: size/type gates, document staging, refusal cards (#54).

Front-ends (TUI, Telegram bridge) feed :class:`MediaInput` items — ``bytes``+MIME
and/or ``path``+modality — through :func:`prepare_media`. Gates live here so every
caller inherits the same allowlist and inline-image cap. Documents are staged in a
fresh :func:`tempfile.mkdtemp` **outside** ``project_root`` (never ``~/.jarn/run/``),
paths are injected into the user turn for ``read_file``, and staging dirs are
deleted after the turn. Unsupported modalities (voice/audio/video/…) produce a
structured :class:`MediaRefusal` (card-ready); caption/text may still proceed.
"""

from __future__ import annotations

import mimetypes
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jarn.agent.files import (
    INLINE_IMAGE_MAX_BYTES,
    image_mime_allowed,
    modality_of,
)

__all__ = [
    "DOCUMENT_MAX_BYTES",
    "INLINE_IMAGE_MAX_BYTES",
    "MediaInput",
    "MediaRefusal",
    "PreparedMedia",
    "StagedDocument",
    "cleanup_staging",
    "expose_staging_roots",
    "inject_document_paths",
    "modality_from_mime",
    "prepare_media",
    "refuse_media",
    "stage_document",
]

#: Telegram's document upload cap — documents may be staged up to this size.
DOCUMENT_MAX_BYTES: int = 50 * 1024 * 1024

#: Explicit document MIME allowlist (plus ``text/*`` via :func:`document_mime_allowed`).
ALLOWED_DOCUMENT_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
        "application/typescript",
        "application/x-sh",
        "application/x-yaml",
        "application/yaml",
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/css",
        "text/xml",
        "text/x-python",
        "text/x-java-source",
        "text/x-c",
        "text/x-c++",
        "text/javascript",
        "text/typescript",
        "text/x-shellscript",
        "text/yaml",
    }
)

#: Modalities that must never enter the model path (voice deferred from v1; #50).
UNSUPPORTED_MODALITIES: frozenset[str] = frozenset(
    {"audio", "voice", "video", "unknown"}
)

RefusalReason = Literal[
    "unsupported_modality",
    "oversize",
    "disallowed_type",
    "unreadable",
]


@dataclass(slots=True, frozen=True)
class MediaInput:
    """One inbound media item for core ingest.

    Provide ``data`` + ``mime`` and/or ``path``. When both are set, ``data`` wins
    for the payload and ``path`` may still supply a filename hint. ``modality``
    overrides MIME/extension classification when given.
    """

    data: bytes | None = None
    mime: str | None = None
    path: str | Path | None = None
    modality: str | None = None
    filename: str | None = None


@dataclass(slots=True, frozen=True)
class MediaRefusal:
    """Structured refusal for a card-ready front-end message (Telegram/TUI)."""

    reason: RefusalReason
    modality: str
    mime: str = ""
    message: str = ""
    filename: str | None = None
    size_bytes: int | None = None
    max_bytes: int | None = None

    def as_event_data(self) -> dict[str, Any]:
        """Payload for an :class:`~jarn.agent.events.Event` ``NOTICE`` / card."""
        data: dict[str, Any] = {
            "media_refusal": True,
            "reason": self.reason,
            "modality": self.modality,
            "mime": self.mime,
            "message": self.message,
        }
        if self.filename is not None:
            data["filename"] = self.filename
        if self.size_bytes is not None:
            data["size_bytes"] = self.size_bytes
        if self.max_bytes is not None:
            data["max_bytes"] = self.max_bytes
        return data


@dataclass(slots=True, frozen=True)
class StagedDocument:
    """A document written into a fresh staging directory outside ``project_root``."""

    path: Path
    staging_dir: Path
    mime: str
    filename: str


@dataclass(slots=True)
class PreparedMedia:
    """Gated, staged result of :func:`prepare_media` for one user turn."""

    text: str
    images: list[Path] = field(default_factory=list)
    documents: list[StagedDocument] = field(default_factory=list)
    refusals: list[MediaRefusal] = field(default_factory=list)
    staging_dirs: list[Path] = field(default_factory=list)

    def cleanup(self) -> None:
        """Delete every staging directory created for this prepare (best-effort)."""
        cleanup_staging(self.staging_dirs)
        self.staging_dirs.clear()
        self.images.clear()
        self.documents.clear()


def modality_from_mime(mime: str) -> str:
    """Map a MIME type to ``image`` / ``document`` / ``audio`` / ``video`` / ``unknown``."""
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    if not normalized:
        return "unknown"
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    if document_mime_allowed(normalized):
        return "document"
    # Common voice containers Telegram uses (.oga) arrive as audio/ogg.
    if normalized in {"application/ogg", "application/octet-stream"}:
        return "unknown"
    return "unknown"


def document_mime_allowed(mime: str) -> bool:
    """True when ``mime`` is on the document allowlist (exact or ``text/*``)."""
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    if not normalized:
        return False
    if normalized in ALLOWED_DOCUMENT_MIMES:
        return True
    return normalized.startswith("text/")


def refuse_media(
    *,
    reason: RefusalReason,
    modality: str,
    mime: str = "",
    filename: str | None = None,
    size_bytes: int | None = None,
    max_bytes: int | None = None,
) -> MediaRefusal:
    """Build a card-ready refusal (unsupported/voice, oversize, or disallowed type)."""
    label = filename or (mime if mime else modality) or "attachment"
    if reason == "unsupported_modality":
        if modality in {"audio", "voice"}:
            message = (
                f"Voice and audio messages are not supported ({label}). "
                "Send text, a photo, or a document (PDF/text) instead."
            )
        elif modality == "video":
            message = (
                f"Video is not supported ({label}). "
                "Send text, a photo, or a document (PDF/text) instead."
            )
        else:
            message = (
                f"This media type is not supported ({label}). "
                "Send text, a photo, or a document (PDF/text) instead."
            )
    elif reason == "oversize":
        kind = "image" if modality == "image" else "file"
        size_lbl = _fmt_bytes(size_bytes) if size_bytes is not None else "too large"
        max_lbl = _fmt_bytes(max_bytes) if max_bytes is not None else "the limit"
        message = (
            f"This {kind} is too large to accept ({label}: {size_lbl}; max {max_lbl})."
        )
    elif reason == "disallowed_type":
        message = (
            f"File type {mime or modality or 'unknown'} is not allowed ({label}). "
            "Send a photo or an allowlisted document (PDF/text)."
        )
    else:  # unreadable
        message = f"Could not read attachment ({label})."
    return MediaRefusal(
        reason=reason,
        modality=modality,
        mime=mime,
        message=message,
        filename=filename,
        size_bytes=size_bytes,
        max_bytes=max_bytes,
    )


def stage_document(
    data: bytes,
    *,
    mime: str,
    filename: str | None = None,
    project_root: Path | None = None,
) -> StagedDocument:
    """Write ``data`` into a fresh ``mkdtemp`` outside ``project_root``.

    Refuses to place staging under ``project_root`` or ``~/.jarn`` (the
    ``find_project_root`` / ``~/.jarn/run`` hazard from #36/#54).
    """
    staging_dir = _mkdtemp_outside_project(project_root)
    safe_name = _safe_filename(filename, mime=mime, default="document")
    dest = staging_dir / safe_name
    dest.write_bytes(data)
    return StagedDocument(
        path=dest, staging_dir=staging_dir, mime=mime, filename=safe_name
    )


def inject_document_paths(text: str, documents: Sequence[StagedDocument]) -> str:
    """Append absolute staged paths so the agent can ``read_file`` them."""
    if not documents:
        return text
    lines = [
        f"Attached document ({doc.mime}): {doc.path.resolve().as_posix()}"
        for doc in documents
    ]
    block = "\n".join(lines)
    base = text.rstrip()
    if base:
        return f"{base}\n\n{block}"
    return block


def cleanup_staging(dirs: Sequence[Path]) -> None:
    """Best-effort recursive delete of staging directories."""
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def expose_staging_roots(backend: Any, staging_dirs: Sequence[Path]) -> Any:
    """Temporarily add staging dirs to a local backend's ``_extra_roots``.

    Under ``virtual_mode=True``, absolute host paths outside ``project_root`` are
    rejected unless listed in ``_extra_roots``. Returns an undo callable that
    restores the prior list (no-op when ``backend`` has no ``_extra_roots``).
    """
    extra = getattr(backend, "_extra_roots", None)
    if extra is None or not staging_dirs:
        return lambda: None
    prior = list(extra)
    resolved = []
    for d in staging_dirs:
        try:
            resolved.append(Path(d).resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    for r in resolved:
        if r not in extra:
            extra.append(r)

    def _undo() -> None:
        current = getattr(backend, "_extra_roots", None)
        if current is None:
            return
        current[:] = prior

    return _undo


def prepare_media(
    text: str,
    items: Sequence[MediaInput] | None,
    *,
    project_root: Path | None = None,
    inline_image_max_bytes: int = INLINE_IMAGE_MAX_BYTES,
    document_max_bytes: int = DOCUMENT_MAX_BYTES,
) -> PreparedMedia:
    """Gate + stage inbound media; return images, injected text, and refusals.

    Images that pass the allowlist and ``inline_image_max_bytes`` become paths for
    :meth:`SessionDriver.run_turn` inlining. Documents that pass the document
    allowlist are staged outside ``project_root`` and their paths are injected into
    ``text``. Unsupported modalities and failed gates yield :class:`MediaRefusal`
    entries — caption/text still proceeds.
    """
    result = PreparedMedia(text=text or "")
    if not items:
        return result

    documents: list[StagedDocument] = []
    for item in items:
        outcome = _ingest_one(
            item,
            project_root=project_root,
            inline_image_max_bytes=inline_image_max_bytes,
            document_max_bytes=document_max_bytes,
        )
        if isinstance(outcome, MediaRefusal):
            result.refusals.append(outcome)
            continue
        kind, payload = outcome
        if kind == "image":
            path, staging = payload
            result.images.append(path)
            if staging is not None:
                result.staging_dirs.append(staging)
        else:
            doc = payload
            documents.append(doc)
            result.documents.append(doc)
            result.staging_dirs.append(doc.staging_dir)

    if documents:
        result.text = inject_document_paths(result.text, documents)
    return result


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _ingest_one(
    item: MediaInput,
    *,
    project_root: Path | None,
    inline_image_max_bytes: int,
    document_max_bytes: int,
) -> MediaRefusal | tuple[str, Any]:
    filename = item.filename
    path = Path(item.path) if item.path is not None else None
    if filename is None and path is not None:
        filename = path.name

    mime = _resolve_mime(item.mime, path=path, filename=filename)
    modality = (item.modality or "").strip().lower() or _resolve_modality(
        mime, path=path, filename=filename
    )

    if modality in UNSUPPORTED_MODALITIES:
        return refuse_media(
            reason="unsupported_modality",
            modality=modality if modality != "unknown" else modality_from_mime(mime),
            mime=mime,
            filename=filename,
        )

    data, read_err = _load_bytes(item.data, path)
    if read_err is not None:
        return read_err

    size = len(data)

    if modality == "image":
        if not image_mime_allowed(mime):
            return refuse_media(
                reason="disallowed_type",
                modality="image",
                mime=mime,
                filename=filename,
            )
        if size > inline_image_max_bytes:
            return refuse_media(
                reason="oversize",
                modality="image",
                mime=mime,
                filename=filename,
                size_bytes=size,
                max_bytes=inline_image_max_bytes,
            )
        if path is not None and item.data is None and path.is_file():
            return ("image", (path, None))
        staging = _mkdtemp_outside_project(project_root)
        safe = _safe_filename(filename, mime=mime, default="image.png")
        dest = staging / safe
        dest.write_bytes(data)
        return ("image", (dest, staging))

    if modality == "document":
        if not document_mime_allowed(mime):
            return refuse_media(
                reason="disallowed_type",
                modality="document",
                mime=mime,
                filename=filename,
            )
        if size > document_max_bytes:
            return refuse_media(
                reason="oversize",
                modality="document",
                mime=mime,
                filename=filename,
                size_bytes=size,
                max_bytes=document_max_bytes,
            )
        doc = stage_document(
            data, mime=mime, filename=filename, project_root=project_root
        )
        return ("document", doc)

    # Treat leftover text-like attachments as documents when MIME allows.
    if document_mime_allowed(mime):
        if size > document_max_bytes:
            return refuse_media(
                reason="oversize",
                modality="document",
                mime=mime,
                filename=filename,
                size_bytes=size,
                max_bytes=document_max_bytes,
            )
        doc = stage_document(
            data, mime=mime, filename=filename, project_root=project_root
        )
        return ("document", doc)

    return refuse_media(
        reason="disallowed_type",
        modality=modality or "unknown",
        mime=mime,
        filename=filename,
    )


def _resolve_mime(
    mime: str | None, *, path: Path | None, filename: str | None
) -> str:
    if mime:
        return mime.split(";", 1)[0].strip().lower()
    hint = filename or (path.name if path is not None else "")
    if hint:
        guessed = mimetypes.guess_type(hint)[0]
        if guessed:
            return guessed.lower()
    if path is not None:
        guessed = mimetypes.guess_type(path.name)[0]
        if guessed:
            return guessed.lower()
    return "application/octet-stream"


def _resolve_modality(
    mime: str, *, path: Path | None, filename: str | None
) -> str:
    from_mime = modality_from_mime(mime)
    if from_mime != "unknown":
        return from_mime
    hint = filename or (path.name if path is not None else "")
    if hint:
        mod = modality_of(hint)
        if mod != "text":
            return mod
        # Plain-text extensions are documents for ingest (read_file path).
        if Path(hint).suffix.lower() in {
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".yaml",
            ".yml",
            ".py",
            ".js",
            ".ts",
            ".html",
            ".css",
            ".xml",
            ".sh",
        }:
            return "document"
    return "unknown"


def _load_bytes(
    data: bytes | None, path: Path | None
) -> tuple[bytes, MediaRefusal | None]:
    if data is not None:
        return data, None
    if path is None:
        return b"", refuse_media(
            reason="unreadable", modality="unknown", mime="", filename=None
        )
    try:
        return Path(path).read_bytes(), None
    except OSError:
        return b"", refuse_media(
            reason="unreadable",
            modality=modality_of(path),
            mime="",
            filename=path.name,
        )


def _mkdtemp_outside_project(project_root: Path | None) -> Path:
    """Create a staging dir in the system temp area; never under root or ``~/.jarn``."""
    staging = Path(tempfile.mkdtemp(prefix="jarn-media-"))
    try:
        _assert_safe_staging(staging, project_root)
    except RuntimeError:
        shutil.rmtree(staging, ignore_errors=True)
        # Force a system temp root that is not the project (rare: project == TMPDIR).
        alt_parent = Path(tempfile.gettempdir()).resolve()
        if project_root is not None:
            try:
                alt_parent.relative_to(Path(project_root).resolve())
                alt_parent = Path("/tmp")  # noqa: S108 - intentional escape hatch
            except ValueError:
                pass
        staging = Path(tempfile.mkdtemp(prefix="jarn-media-", dir=str(alt_parent)))
        _assert_safe_staging(staging, project_root)
    return staging


def _assert_safe_staging(staging: Path, project_root: Path | None) -> None:
    resolved = staging.resolve()
    if project_root is not None:
        try:
            resolved.relative_to(Path(project_root).expanduser().resolve())
        except ValueError:
            pass
        else:
            raise RuntimeError(
                f"refusing to stage media inside project_root ({project_root})"
            )
    jarn_home = (Path.home() / ".jarn").resolve()
    try:
        resolved.relative_to(jarn_home)
    except ValueError:
        return
    raise RuntimeError("refusing to stage media under ~/.jarn")


def _safe_filename(filename: str | None, *, mime: str, default: str) -> str:
    raw = (filename or "").strip().replace("\x00", "")
    name = Path(raw).name if raw else ""
    if not name or name in {".", ".."}:
        ext = mimetypes.guess_extension(mime.split(";", 1)[0].strip().lower()) or ""
        name = default if default.endswith(ext) or not ext else f"document{ext}"
        if name == "document" and ext:
            name = f"document{ext}"
    # Flatten any residual path separators from hostile names.
    return name.replace("/", "_").replace("\\", "_")


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
