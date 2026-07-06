"""FIFO input queue for lines submitted while a turn is running."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class QueuedLine:
    display: str
    payload: str
    #: True for programmatically-queued internal items (e.g. diagnostics auto-fix
    #: rounds) that were never echoed as ``» queued: …`` to the user.
    internal: bool = False


class InputQueue:
    """(display, payload) queue — display is echoed; payload is what runs."""

    def __init__(self) -> None:
        self._items: list[QueuedLine] = []

    def append(self, display: str, payload: str, internal: bool = False) -> int:
        self._items.append(
            QueuedLine(display=display, payload=payload, internal=internal)
        )
        return len(self._items)

    def pop_next(self) -> QueuedLine | None:
        if not self._items:
            return None
        return self._items.pop(0)

    def clear(self) -> int:
        n = len(self._items)
        self._items.clear()
        return n

    def cancel(self, index: int) -> QueuedLine | None:
        """Remove item at 1-based ``index``; returns removed line or ``None``."""
        if index < 1 or index > len(self._items):
            return None
        return self._items.pop(index - 1)

    def move(self, from_index: int, to_index: int) -> bool:
        """Reorder using 1-based indices."""
        if from_index < 1 or from_index > len(self._items):
            return False
        if to_index < 1 or to_index > len(self._items):
            return False
        if from_index == to_index:
            return True
        item = self._items.pop(from_index - 1)
        self._items.insert(to_index - 1, item)
        return True

    def list(self) -> list[QueuedLine]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)
