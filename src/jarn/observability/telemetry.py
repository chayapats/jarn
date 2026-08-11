"""Opt-in, privacy-respecting telemetry.

Design principles for v1:

* **Default OFF.** Nothing is recorded unless ``observability.telemetry: true``.
* **Local only.** Events are appended to ``~/.jarn/telemetry.jsonl`` on disk.
  There is no network upload in v1 (a remote sink is a future, separately
  opt-in roadmap item).
* **Anonymized.** Only event *names* and *numeric* properties are recorded —
  never prompts, file contents, paths, commands, or model outputs. A random,
  locally-stored install id allows de-duplicating runs without identifying you.

Disabled instances are hard no-ops, so call sites need no guards.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarn.config import paths
from jarn.config.secrets import redact_secrets, redact_structure
from jarn.util.atomic import atomic_write_text, file_lock

# Event and metric labels are data too: accepting arbitrary labels would let a
# caller smuggle prompts, paths, or credentials into an otherwise numeric sink.
_EVENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_METRIC_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
_RESERVED_KEYS = frozenset({"event", "ts", "install"})


@dataclass(frozen=True, slots=True)
class _JsonlScan:
    size_bytes: int
    valid_events: int
    corrupt_lines: tuple[int, ...]
    final_record_line: int | None
    final_record_valid: bool | None
    repair_offset: int | None
    missing_final_newline: bool

    @property
    def corruption_detected(self) -> bool:
        return bool(self.corrupt_lines)


def _numeric(value: Any) -> int | float | bool | None:
    """Return an allowed JSON metric or ``None`` for everything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _safe_event_name(value: Any) -> str:
    raw = str(value)
    # Use the central redactor as the canonical secret detector.  Do not persist
    # even its partially masked form: telemetry has no need to identify a key.
    if redact_secrets(raw) != raw or _EVENT_NAME_RE.fullmatch(raw) is None:
        return "redacted_event"
    return raw


def _safe_install_id(value: Any) -> str:
    raw = str(value)
    # Direct construction historically allowed an empty id; from_config() still
    # supplies a real UUID whenever telemetry is enabled.
    if not raw:
        return ""
    if redact_secrets(raw) != raw or _INSTALL_ID_RE.fullmatch(raw) is None:
        return "redacted"
    return raw


def _safe_property(name: Any, value: Any) -> tuple[str, int | float | bool] | None:
    key = str(name)
    if key in _RESERVED_KEYS or _METRIC_NAME_RE.fullmatch(key) is None:
        return None
    numeric = _numeric(value)
    if numeric is None:
        return None
    # Credential-shaped field names are scrubbed centrally even when the value
    # happens to be numeric (PINs and token identifiers can be numbers).
    redacted = redact_structure({key: numeric})
    if not isinstance(redacted, dict) or redacted.get(key) != numeric:
        return None
    if redact_secrets(key) != key:
        return None
    return key, numeric


def _normalise_row(row: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = _numeric(row.get("ts"))
    if timestamp is None or isinstance(timestamp, bool):
        return None
    safe: dict[str, Any] = {
        "event": _safe_event_name(row.get("event", "redacted_event")),
        "ts": timestamp,
        "install": _safe_install_id(row.get("install", "")),
    }
    for key, value in row.items():
        prop = _safe_property(key, value)
        if prop is not None:
            safe[prop[0]] = prop[1]
    return safe


def _valid_persisted_row(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    required = {"event", "ts", "install"}
    if not required.issubset(value):
        return False
    normalised = _normalise_row(value)
    return normalised is not None and normalised == value


def _line_content(segment: bytes) -> bytes:
    if segment.endswith(b"\r\n"):
        return segment[:-2]
    if segment.endswith((b"\n", b"\r")):
        return segment[:-1]
    return segment


def _has_valid_event_prefix(content: bytes) -> bool:
    """True when a malformed line begins with a complete valid event.

    Such a line is still corrupt JSONL, but truncating it would silently discard
    a valid event prefix. Leave it byte-for-byte intact for explicit recovery.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        try:
            text = content[: exc.start].decode("utf-8")
        except UnicodeDecodeError:
            return False
    start = len(text) - len(text.lstrip())
    try:
        value, _end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return False
    return _valid_persisted_row(value)


def _scan_jsonl(data: bytes) -> _JsonlScan:
    """Classify records without ever including their content in diagnostics."""
    valid_events = 0
    corrupt: list[int] = []
    records: list[tuple[int, int, bool, bytes]] = []
    offset = 0
    for line_number, segment in enumerate(data.splitlines(keepends=True), start=1):
        content = _line_content(segment)
        if content.strip():
            try:
                decoded = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                valid = False
            else:
                valid = _valid_persisted_row(decoded)
            records.append((line_number, offset, valid, content))
            if valid:
                valid_events += 1
            else:
                corrupt.append(line_number)
        offset += len(segment)

    final_record_line: int | None = None
    final_record_valid: bool | None = None
    repair_offset: int | None = None
    if records:
        final_record_line, final_offset, final_valid, final_content = records[-1]
        final_record_valid = final_valid
        if not final_valid and not _has_valid_event_prefix(final_content):
            # Only bytes belonging to the final non-empty record (and blank bytes
            # after it) are repairable. Earlier malformed records are preserved.
            repair_offset = final_offset
    return _JsonlScan(
        size_bytes=len(data),
        valid_events=valid_events,
        corrupt_lines=tuple(corrupt),
        final_record_line=final_record_line,
        final_record_valid=final_record_valid,
        repair_offset=repair_offset,
        missing_final_newline=bool(data) and not data.endswith(b"\n"),
    )


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("telemetry append made no progress")
        remaining = remaining[written:]


def _encode_rows(rows: list[dict[str, Any]]) -> bytes:
    encoded: list[bytes] = []
    for row in rows:
        normalised = _normalise_row(row)
        if normalised is None:
            continue
        encoded.append(
            json.dumps(
                normalised,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    return b"".join(encoded)


@dataclass(slots=True)
class Telemetry:
    enabled: bool = False
    sink_path: Path | None = None
    install_id: str = ""
    _buffer: list[dict[str, Any]] = field(default_factory=list)
    _last_recovery: str = field(default="", init=False, repr=False)
    _last_flush_error: str = field(default="", init=False, repr=False)

    @classmethod
    def from_config(cls, enabled: bool) -> Telemetry:
        sink = paths.global_home() / "telemetry.jsonl"
        return cls(enabled=enabled, sink_path=sink, install_id=_install_id() if enabled else "")

    def record(self, event: str, *, when: float, **props: Any) -> None:
        """Record an anonymized event. No-op when disabled.

        ``when`` is passed in explicitly (no implicit clock) for determinism.
        Non-numeric props are silently dropped to prevent leaking content.
        """
        if not self.enabled:
            return
        timestamp = _numeric(when)
        if timestamp is None or isinstance(timestamp, bool):
            return
        safe: dict[str, Any] = {}
        for key, value in props.items():
            prop = _safe_property(key, value)
            if prop is not None:
                safe[prop[0]] = prop[1]
        self._buffer.append(
            {
                "event": _safe_event_name(event),
                "ts": round(timestamp, 3),
                "install": _safe_install_id(self.install_id),
                **safe,
            }
        )

    def flush(self) -> None:
        if not self.enabled or not self._buffer or self.sink_path is None:
            self._buffer.clear()
            return
        payload = _encode_rows(self._buffer)
        if not payload:
            self._buffer.clear()
            return
        path = Path(self.sink_path)
        if path.is_symlink():
            self._last_flush_error = "refused to append telemetry through a symbolic link"
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(path) as locked:
                if not locked:
                    self._last_flush_error = "could not acquire the telemetry write lock"
                    return
                flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
                flags |= getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags, 0o600)
                try:
                    if os.name != "nt":
                        os.fchmod(fd, 0o600)
                    data = _read_fd(fd)
                    scan = _scan_jsonl(data)
                    if scan.repair_offset is not None:
                        os.ftruncate(fd, scan.repair_offset)
                        data = data[: scan.repair_offset]
                        self._last_recovery = (
                            "removed malformed final telemetry record "
                            f"at line {scan.final_record_line}; preserved "
                            f"{scan.valid_events} valid event(s)"
                        )
                    if data and not data.endswith(b"\n"):
                        payload = b"\n" + payload
                    if scan.missing_final_newline and scan.final_record_valid:
                        self._last_recovery = (
                            "completed the missing newline after a valid final "
                            "telemetry record before append"
                        )

                    remaining_corruption = len(scan.corrupt_lines) - int(
                        scan.repair_offset is not None
                    )
                    if remaining_corruption:
                        self._last_flush_error = (
                            f"preserved {remaining_corruption} malformed non-final "
                            "telemetry record(s); only the final record is auto-repairable"
                        )
                    else:
                        self._last_flush_error = ""
                    os.lseek(fd, 0, os.SEEK_END)
                    _write_all(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            # Opt-in telemetry must never take down the user command. Keep the
            # buffer for a later retry and expose a centrally-redacted status.
            self._last_flush_error = redact_secrets(str(exc))
            return
        self._buffer.clear()

    def status_summary(self) -> dict[str, Any]:
        """User-facing telemetry audit snapshot for ``/telemetry status``."""
        path = self.sink_path
        size_bytes = 0
        event_count = 0
        corrupt_lines: tuple[int, ...] = ()
        repairable_final_record = False
        read_error = ""
        if path is not None and path.is_file():
            try:
                with file_lock(path):
                    scan = _scan_jsonl(path.read_bytes())
                size_bytes = scan.size_bytes
                event_count = scan.valid_events
                corrupt_lines = scan.corrupt_lines
                repairable_final_record = scan.repair_offset is not None
            except OSError as exc:
                read_error = redact_secrets(str(exc))
        install_path = paths.global_home() / ".install_id"
        install_present = bool(self.install_id) or install_path.is_file()
        error = read_error or self._last_flush_error
        corruption_detected = bool(corrupt_lines)
        if corruption_detected:
            health = "corrupt"
        elif error:
            health = "degraded"
        elif self._last_recovery:
            health = "recovered"
        else:
            health = "healthy"
        return {
            "enabled": self.enabled,
            "path": str(path) if path is not None else "",
            "size_bytes": size_bytes,
            "event_count": event_count,
            "valid_event_count": event_count,
            "corrupt_record_count": len(corrupt_lines),
            "corrupt_record_lines": list(corrupt_lines),
            "corruption_detected": corruption_detected,
            "repairable_final_record": repairable_final_record,
            "recovery_performed": bool(self._last_recovery),
            "recovery_message": self._last_recovery,
            "last_error": error,
            "health": health,
            "install_id_present": install_present,
        }


def _install_id() -> str:
    """Stable anonymous id, atomically published under an exclusive lock."""
    path = paths.global_home() / ".install_id"
    new_id = uuid.uuid4().hex
    if path.is_symlink():
        return new_id
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path) as locked:
            if not locked:
                return new_id
            if path.is_file():
                existing = path.read_text(encoding="utf-8").strip()
                if _UUID_HEX_RE.fullmatch(existing):
                    if os.name != "nt":
                        path.chmod(0o600)
                    return existing
            atomic_write_text(path, new_id, mode=0o600)
            if os.name != "nt":
                path.chmod(0o600)
    except OSError:
        return new_id
    return new_id
