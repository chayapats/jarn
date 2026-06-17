"""Input completion logic for the prompt — ``/command`` and ``@`` mentions.

Kept UI-free and deterministic so it can be unit-tested; the Textual layer just
renders the returned candidates and applies the chosen replacement.

``@`` mentions are extensible via a small resolver registry.  A bare ``@frag``
completes file paths (the common case, unchanged); an explicit ``@kind:frag``
routes to a registered resolver.  The shipped kinds are local and synchronous:

  * ``@frag``          → file/dir paths (FileMentionResolver, kind "file")
  * ``@folder:frag``   → directories only (FolderMentionResolver, kind "folder")
  * ``@symbol:frag``   → symbols from the repo map (SymbolMentionResolver,
                         kind "symbol"); replacement is the agent-readable
                         ``@<rel>:<symbol>`` token.

These are completion-only authoring aids: they insert a precise token that the
agent resolves with its existing read tools; nothing is pre-expanded into the
prompt.  TODO(rich-at-mentions): later slices add ``@git`` (sync), then content
pre-expansion on submit, then ``@url`` / ``@docs`` (async fetch + cache) — each
needs a different (non-per-keystroke / async) surface, so they are out of scope
here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jarn.agent.repomap import build_symbol_index

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
    kind: str  # "command" | "file" | "folder" | "symbol"
    description: str = ""


@dataclass(slots=True, frozen=True)
class _DirCacheEntry:
    """A cached, sorted directory listing plus its freshness keys."""

    entries: tuple[Path, ...]
    mtime: float
    stamp: float


class MentionResolver(Protocol):
    """Resolves one ``@`` mention kind into completion candidates.

    ``resolve`` is given the input text *before* the mention token, the
    fragment typed after the kind prefix, and the project ``root``; it returns
    the candidate list (each ``replacement`` is the full new input text).
    Resolution must be synchronous and fast — it runs on every keystroke.
    """

    # Read-only so frozen-dataclass resolvers (whose ``kind`` attribute is
    # immutable) structurally satisfy the protocol.
    @property
    def kind(self) -> str: ...

    def resolve(
        self, provider: CompletionProvider, prefix_text: str, frag: str, root: Path
    ) -> list[Completion]: ...


@dataclass(slots=True)
class CompletionProvider:
    """Slash-command and ``@`` mention completion for the REPL prompt."""

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

        # Mention completion: the last whitespace-token starts with "@".
        token = text.rsplit(" ", 1)[-1] if " " in text else text
        if token.startswith("@"):
            prefix = text[: len(text) - len(token)]
            return self._mentions(prefix, token[1:])
        return []

    def _mentions(self, prefix_text: str, frag: str) -> list[Completion]:
        """Route an ``@`` token to a resolver.

        ``@kind:rest`` routes to the resolver registered for ``kind`` when the
        segment before the FIRST colon is a known kind keyword; everything else
        (including a bare ``@frag`` and any unknown ``@kind:`` prefix) falls
        through to the file resolver — so the well-tested bare-``@`` file UX is
        byte-for-byte unchanged.  Note the ``@symbol`` replacement embeds its
        own ``:`` (``@rel:symbol``); routing only ever splits on the FIRST
        colon, so that does not collide.
        """
        root = self.project_root or Path.cwd()
        if ":" in frag:
            kind, _, rest = frag.partition(":")
            resolver = _RESOLVERS.get(kind)
            if resolver is not None:
                return resolver.resolve(self, prefix_text, rest, root)
        return _FILE_RESOLVER.resolve(self, prefix_text, frag, root)

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


# ---------------------------------------------------------------------------
# Mention resolvers
# ---------------------------------------------------------------------------


def _walk_entries(
    provider: CompletionProvider,
    prefix_text: str,
    frag: str,
    root: Path,
    *,
    dirs_only: bool,
    kind: str,
) -> list[Completion]:
    """Shared directory walk for the file and folder resolvers.

    Lists entries of the directory implied by ``frag`` (its parent part) whose
    name matches the trailing part (case-insensitive), skipping dotfiles unless
    the fragment itself is dotted.  ``dirs_only`` filters to directories.
    """
    if not root.is_dir():
        return []
    frag_path = Path(frag)
    search_dir = root / frag_path.parent if frag_path.parent != Path(".") else root
    name_prefix = frag_path.name
    if not search_dir.is_dir():
        return []

    entries = provider._listing(search_dir)
    if entries is None:
        return []

    out: list[Completion] = []
    for entry in entries:
        if dirs_only and not entry.is_dir():
            continue
        if entry.name.startswith(".") and not name_prefix.startswith("."):
            continue
        if not entry.name.lower().startswith(name_prefix.lower()):
            continue
        rel = entry.relative_to(root)
        suffix = "/" if entry.is_dir() else ""
        replacement = f"{prefix_text}@{rel}{suffix}"
        out.append(Completion(f"@{rel}{suffix}", replacement, kind))
        if len(out) >= provider.max_files:
            break
    return out


@dataclass(slots=True, frozen=True)
class FileMentionResolver:
    """``@frag`` → file/dir paths (the default, bare-``@`` mention)."""

    kind: str = "file"

    def resolve(
        self, provider: CompletionProvider, prefix_text: str, frag: str, root: Path
    ) -> list[Completion]:
        return _walk_entries(
            provider, prefix_text, frag, root, dirs_only=False, kind=self.kind
        )


@dataclass(slots=True, frozen=True)
class FolderMentionResolver:
    """``@folder:frag`` → directories only (replacement is the ``@<rel>/`` token)."""

    kind: str = "folder"

    def resolve(
        self, provider: CompletionProvider, prefix_text: str, frag: str, root: Path
    ) -> list[Completion]:
        return _walk_entries(
            provider, prefix_text, frag, root, dirs_only=True, kind=self.kind
        )


@dataclass(slots=True, frozen=True)
class SymbolMentionResolver:
    """``@symbol:frag`` → repo-map symbols (replacement is ``@<rel>:<symbol>``).

    Backed by the module-cached :func:`build_symbol_index` so the per-keystroke
    lookup is O(prefix-match), not O(repo).  Matches case-insensitively by
    prefix, caps at ``max_files`` like files, and surfaces the rel path as the
    description so duplicate names disambiguate.  The menu label embeds the
    container (``Class.method``) when present; the inserted token is the
    path-anchored ``@<rel>:<symbol>`` form the agent can read.
    """

    kind: str = "symbol"

    def resolve(
        self, provider: CompletionProvider, prefix_text: str, frag: str, root: Path
    ) -> list[Completion]:
        if not root.is_dir():
            return []
        frag_lower = frag.lower()
        out: list[Completion] = []
        for ref in build_symbol_index(root):
            if not ref.name.lower().startswith(frag_lower):
                continue
            qualified = f"{ref.container}.{ref.name}" if ref.container else ref.name
            label = f"@{ref.rel}:{qualified}"
            replacement = f"{prefix_text}@{ref.rel}:{ref.name}"
            out.append(Completion(label, replacement, self.kind, description=ref.rel))
            if len(out) >= provider.max_files:
                break
        return out


#: The default resolver for a bare ``@frag`` token.
_FILE_RESOLVER: FileMentionResolver = FileMentionResolver()

def _build_registry(*resolvers: MentionResolver) -> dict[str, MentionResolver]:
    """Index ``resolvers`` by their ``kind`` (keeps Protocol typing precise)."""
    return {r.kind: r for r in resolvers}


#: Registry of explicit ``@kind:`` mention resolvers, keyed by each resolver's
#: ``kind``.  Additive — new kinds (e.g. ``@git`` in a later slice) register
#: here without touching routing or the bare-``@`` file path.
_RESOLVERS: dict[str, MentionResolver] = _build_registry(
    FolderMentionResolver(),
    SymbolMentionResolver(),
)
