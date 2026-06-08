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
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarn.config import paths

#: Numeric-only props are allowed through; everything else is dropped.
_ALLOWED_TYPES = (int, float, bool)


@dataclass(slots=True)
class Telemetry:
    enabled: bool = False
    sink_path: Path | None = None
    install_id: str = ""
    _buffer: list[dict[str, Any]] = field(default_factory=list)

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
        safe = {k: v for k, v in props.items() if isinstance(v, _ALLOWED_TYPES)}
        self._buffer.append({
            "event": str(event),
            "ts": round(when, 3),
            "install": self.install_id,
            **safe,
        })

    def flush(self) -> None:
        if not self.enabled or not self._buffer or self.sink_path is None:
            self._buffer.clear()
            return
        self.sink_path.parent.mkdir(parents=True, exist_ok=True)
        with self.sink_path.open("a", encoding="utf-8") as fh:
            for row in self._buffer:
                fh.write(json.dumps(row) + "\n")
        self._buffer.clear()


def _install_id() -> str:
    """Stable, anonymous per-install id stored locally."""
    path = paths.global_home() / ".install_id"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    new_id = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id, encoding="utf-8")
    except OSError:
        pass
    return new_id
