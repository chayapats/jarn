"""Input completion logic for the prompt — ``/command`` and ``@file`` candidates.

Kept UI-free and deterministic so it can be unit-tested; the Textual layer just
renders the returned candidates and applies the chosen replacement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

# How long (seconds) a cached directory listing stays valid for. Short enough
# that fresh files show up almost immediately, long enough that a burst of
# keystrokes in the same directory reuses one scan instead of re-running
# ``iterdir()`` per character.
_DIR_CACHE_TTL = 1.0


@dataclass(slots=True, frozen=True)
class Completion:
    """A single candidate. ``replacement`` is the full new input text if chosen."""

    label: str
    replacement: str
    kind: str  # "command" | "file"
    description: str = ""


@dataclass(slots=True, frozen=True)
class _DirCacheEntry:
    """A cached, sorted directory listing plus its freshness keys."""

    entries: tuple[Path, ...]
    mtime: float
    stamp: float


@dataclass(slots=True)
class CompletionProvider:
    """Slash-command and ``@file`` completion for the REPL prompt."""

    command_catalog: dict[str, str]  # name → short description
    project_root: Path | None = None
    max_files: int = 12
    _dir_cache: dict[Path, _DirCacheEntry] = field(default_factory=dict, repr=False)

    @property
    def commands(self) -> list[str]:
        """Command names (for tests and legacy callers)."""
        return sorted(self.command_catalog)

    def complete(self, text: str) -> list[Completion]:
        """Return candidates for the current single-line input ``text``."""
        # Command completion: the whole line is "/prefix" with no space yet.
        if text.startswith("/") and " " not in text:
            return self._commands(text[1:])

        # File completion: the last whitespace-token starts with "@".
        token = text.rsplit(" ", 1)[-1] if " " in text else text
        if token.startswith("@"):
            prefix = text[: len(text) - len(token)]
            return self._files(prefix, token[1:])
        return []

    def _commands(self, prefix: str) -> list[Completion]:
        prefix = prefix.lower()
        out: list[Completion] = []
        for name in sorted(self.command_catalog):
            if name.lower().startswith(prefix):
                out.append(
                    Completion(
                        f"/{name}",
                        f"/{name} ",
                        "command",
                        description=self.command_catalog.get(name, ""),
                    )
                )
        return out

    def _files(self, prefix_text: str, frag: str) -> list[Completion]:
        root = self.project_root or Path.cwd()
        if not root.is_dir():
            return []
        # Split the fragment into a directory part and a name part.
        frag_path = Path(frag)
        search_dir = root / frag_path.parent if frag_path.parent != Path(".") else root
        name_prefix = frag_path.name
        if not search_dir.is_dir():
            return []

        entries = self._listing(search_dir)
        if entries is None:
            return []

        out: list[Completion] = []
        for entry in entries:
            if entry.name.startswith(".") and not name_prefix.startswith("."):
                continue
            if not entry.name.lower().startswith(name_prefix.lower()):
                continue
            rel = entry.relative_to(root)
            suffix = "/" if entry.is_dir() else ""
            replacement = f"{prefix_text}@{rel}{suffix}"
            out.append(Completion(f"@{rel}{suffix}", replacement, "file"))
            if len(out) >= self.max_files:
                break
        return out

    def _listing(self, search_dir: Path) -> tuple[Path, ...] | None:
        """Return the sorted entries of ``search_dir``, reusing a brief cache.

        A burst of keystrokes typed into the same directory reuses one scan
        instead of calling ``iterdir()`` (a full scan + sort) per character.
        The cache is keyed on the directory path and invalidated by both a
        short TTL and the directory's mtime, so freshly created files appear
        promptly and the returned candidates are identical to an uncached scan.
        """
        key = search_dir
        now = time.monotonic()
        try:
            mtime = search_dir.stat().st_mtime
        except OSError:
            self._dir_cache.pop(key, None)
            return None

        cached = self._dir_cache.get(key)
        if (
            cached is not None
            and cached.mtime == mtime
            and now - cached.stamp < _DIR_CACHE_TTL
        ):
            return cached.entries

        try:
            entries = tuple(
                sorted(search_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            )
        except OSError:
            self._dir_cache.pop(key, None)
            return None
        self._dir_cache[key] = _DirCacheEntry(entries=entries, mtime=mtime, stamp=now)
        return entries
