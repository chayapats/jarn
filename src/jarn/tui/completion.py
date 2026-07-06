"""Input completion logic for the prompt — ``/command`` and ``@`` mentions.

Kept UI-free and deterministic so it can be unit-tested; the Textual layer just
renders the returned candidates and applies the chosen replacement.

``@`` mentions are extensible via a small resolver registry.  A bare ``@frag``
completes file paths (the common case, unchanged); an explicit ``@kind:frag``
routes to a registered resolver.  The shipped kinds are:

  * ``@frag``          → file/dir paths (FileMentionResolver, kind "file")
  * ``@folder:frag``   → directories only (FolderMentionResolver, kind "folder")
  * ``@symbol:frag``   → symbols from the repo map (SymbolMentionResolver,
                         kind "symbol"); replacement is the agent-readable
                         ``@<rel>:<symbol>`` token.
  * ``@git:subcmd``    → completes status/diff/staged/log; at submit time
                         ``expand_mentions`` runs the real command and injects
                         the output as a ``<git-mention>`` block
                         (GitMentionResolver, kind "git").
  * ``@url:<url>``     → no keystroke completions (freeform URL); at submit time
                         ``expand_mentions`` rewrites to a web_fetch instruction
                         (UrlMentionResolver, kind "url").
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jarn.agent.repomap import build_symbol_index
from jarn.config.profiles import PROFILES

_MODE_CHOICES = ("plan", "ask", "auto-edit", "yolo")

# How long (seconds) a cached directory listing stays valid for. Short enough
# that fresh files show up almost immediately, long enough that a burst of
# keystrokes in the same directory reuses one scan instead of re-running
# ``iterdir()`` per character.
_DIR_CACHE_TTL = 1.0

# Characters that mark the start of a new "word" inside a candidate string.
# A query character matching right after one of these (or at position 0) earns
# the word-boundary bonus in the fuzzy scorer.
_WB: frozenset[str] = frozenset("-_./ ")

# Maximum number of candidates returned by the two-tier pipeline (tier 1 prefix
# matches + tier 2 fuzzy-only matches combined).
_CAP: int = 10


# ---------------------------------------------------------------------------
# Fuzzy scoring helpers
# ---------------------------------------------------------------------------


def _fuzzy_score(query: str, candidate: str) -> float | None:
    """Score ``query`` as a subsequence match inside ``candidate``.

    Returns a float score when every character of ``query`` appears in
    ``candidate`` in order (case-insensitive), or ``None`` when the match
    fails.  Higher is better.

    Scoring per matched character:
      * +3  — word-boundary hit: position 0 or preceded by a char in ``_WB``
      * +1  — adjacent-run continuation (matched at ``prev_ci + 1``)
      * −0.1 × gap — otherwise, proportional to skipped candidate characters
    """
    q = query.lower()
    c = candidate.lower()
    score = 0.0
    qi = 0
    prev_ci: int | None = None

    for ci, ch in enumerate(c):
        if qi >= len(q):
            break
        if ch == q[qi]:
            is_wb = ci == 0 or c[ci - 1] in _WB
            if is_wb:
                score += 3.0
            elif prev_ci is not None and ci == prev_ci + 1:
                score += 1.0  # adjacent run
            else:
                gap = ci - (prev_ci + 1 if prev_ci is not None else 0)
                score -= 0.1 * gap
            prev_ci = ci
            qi += 1

    return score if qi == len(q) else None


def fuzzy_rank(query: str, candidates: list[str]) -> list[str]:
    """Return the subset of ``candidates`` that match ``query`` as a subsequence.

    Results are sorted best-first: highest score first, ties broken
    alphabetically.  Pure and side-effect-free — safe to call on every
    keystroke over the already-listed candidates (typically ≤ a few hundred).

    ``query`` is matched case-insensitively.  An empty ``query`` returns all
    candidates in their original order (no filtering, no reranking).
    """
    if not query:
        return list(candidates)
    scored: list[tuple[float, str, str]] = []
    for cand in candidates:
        s = _fuzzy_score(query, cand)
        if s is not None:
            scored.append((s, cand.lower(), cand))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in scored]


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
    model_refs: list[str] | None = None
    preset_names: list[str] | None = None
    session_titles: list[str] | None = None
    mcp_servers: list[str] | None = None
    _dir_cache: dict[Path, _DirCacheEntry] = field(default_factory=dict, repr=False)

    @property
    def commands(self) -> list[str]:
        """Command names (for tests and legacy callers)."""
        return sorted(self.command_catalog)

    def complete(self, text: str) -> list[Completion]:
        """Return candidates for the current single-line input ``text``."""
        if text.startswith("/"):
            if " " not in text:
                return self._commands(text[1:])
            cmd, _, arg_frag = text.partition(" ")
            return self._command_args(cmd[1:], arg_frag, f"{cmd} ")

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
        prefix_cf = prefix.lower()

        # Tier 1 — prefix matches, sorted alphabetically (original behaviour).
        tier1: list[str] = []
        tier1_set: set[str] = set()
        for name in sorted(self.command_catalog):
            if name.lower().startswith(prefix_cf):
                tier1.append(name)
                tier1_set.add(name)

        # Tier 2 — fuzzy matches not already in tier 1, ranked by score.
        remaining = [n for n in self.command_catalog if n not in tier1_set]
        slots = max(0, _CAP - len(tier1))
        tier2 = fuzzy_rank(prefix, remaining)[:slots]

        out: list[Completion] = []
        for name in tier1 + tier2:
            if len(out) >= _CAP:
                break
            out.append(
                Completion(
                    f"/{name}",
                    f"/{name} ",
                    "command",
                    description=self.command_catalog.get(name, ""),
                )
            )
        return out

    def _command_args(self, cmd: str, frag: str, prefix: str) -> list[Completion]:
        """Complete the argument fragment after ``/cmd ``."""
        choices: list[str] | None
        match cmd.lower():
            case "model":
                choices = self.model_refs
            case "mode":
                choices = list(_MODE_CHOICES)
            case "preset":
                choices = (
                    self.preset_names
                    if self.preset_names is not None
                    else sorted(PROFILES)
                )
            case "resume" | "sessions":
                choices = self.session_titles
            case "mcp":
                choices = self.mcp_servers
            case _:
                return []

        if not choices:
            return []

        frag_cf = frag.casefold()
        sorted_choices = sorted(choices, key=str.casefold)

        # Tier 1 — case-insensitive prefix matches (original sorted order).
        tier1: list[str] = []
        tier1_set: set[str] = set()
        for choice in sorted_choices:
            if choice.casefold().startswith(frag_cf):
                tier1.append(choice)
                tier1_set.add(choice)

        # Tier 2 — fuzzy matches not already in tier 1, ranked by score.
        remaining = [c for c in sorted_choices if c not in tier1_set]
        slots = max(0, _CAP - len(tier1))
        tier2 = fuzzy_rank(frag, remaining)[:slots]

        out: list[Completion] = []
        for choice in tier1 + tier2:
            if len(out) >= _CAP:
                break
            out.append(Completion(choice, f"{prefix}{choice}", "argument"))
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

    name_prefix_cf = name_prefix.lower()

    def _is_visible(entry: Path) -> bool:
        """Return True when the entry passes dotfile and dirs-only filters."""
        if dirs_only and not entry.is_dir():
            return False
        return not (entry.name.startswith(".") and not name_prefix.startswith("."))

    # Tier 1 — prefix matches (same filter and order as the original code).
    tier1: list[Path] = []
    tier1_names: set[str] = set()
    for entry in entries:
        if not _is_visible(entry):
            continue
        if entry.name.lower().startswith(name_prefix_cf):
            tier1.append(entry)
            tier1_names.add(entry.name)

    # Tier 2 — fuzzy matches on the name segment, not already in tier 1.
    # Only apply when the user typed a non-empty name fragment; an empty
    # fragment (bare ``@``) already lists everything via tier 1.
    tier2: list[Path] = []
    if name_prefix:
        pool = [e for e in entries if _is_visible(e) and e.name not in tier1_names]
        ranked_names = fuzzy_rank(name_prefix, [e.name for e in pool])
        name_to_entry = {e.name: e for e in pool}
        tier2 = [name_to_entry[n] for n in ranked_names if n in name_to_entry]

    cap = provider.max_files
    out: list[Completion] = []
    for entry in tier1 + tier2:
        if len(out) >= cap:
            break
        rel_str = entry.relative_to(root).as_posix()
        suffix = "/" if entry.is_dir() else ""
        replacement = f"{prefix_text}@{rel_str}{suffix}"
        out.append(Completion(f"@{rel_str}{suffix}", replacement, kind))
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
        if not root.is_dir() or not frag:
            # An empty fragment would match every symbol in arbitrary order; wait
            # for at least one character (same as how unknown prefixes return []).
            return []
        frag_cf = frag.casefold()
        all_refs = list(build_symbol_index(root))

        # Tier 1 — case-insensitive prefix matches, sorted deterministically
        # (by name, then file, then container) BEFORE the cap — otherwise the
        # max_files cut drops matches in arbitrary git-ls-files order.
        tier1 = sorted(
            [r for r in all_refs if r.name.casefold().startswith(frag_cf)],
            key=lambda r: (r.name.casefold(), r.rel, r.container),
        )
        tier1_names_cf = frozenset(r.name.casefold() for r in tier1)

        # Tier 2 — fuzzy matches on symbol names not already covered by tier 1.
        tier2_pool = [r for r in all_refs if r.name.casefold() not in tier1_names_cf]
        unique_names = list(dict.fromkeys(r.name for r in tier2_pool))
        ranked_names = fuzzy_rank(frag, unique_names)
        rank_idx = {n.casefold(): i for i, n in enumerate(ranked_names)}
        tier2 = sorted(
            [r for r in tier2_pool if r.name.casefold() in rank_idx],
            key=lambda r: (rank_idx[r.name.casefold()], r.name.casefold(), r.rel, r.container),
        )

        out: list[Completion] = []
        for ref in (tier1 + tier2)[: provider.max_files]:
            qualified = f"{ref.container}.{ref.name}" if ref.container else ref.name
            label = f"@{ref.rel}:{qualified}"
            replacement = f"{prefix_text}@{ref.rel}:{ref.name}"
            out.append(Completion(label, replacement, self.kind, description=ref.rel))
        return out


@dataclass(slots=True, frozen=True)
class GitMentionResolver:
    """``@git:subcmd`` → completes the 4 read-only subcommands.

    Completion-time: returns the known subcommands filtered by the typed
    fragment.  At submit time ``expand_mentions`` runs the real git command
    and replaces the token with the output (see the module-level function).
    """

    kind: str = "git"

    def resolve(
        self, provider: CompletionProvider, prefix_text: str, frag: str, root: Path
    ) -> list[Completion]:
        frag_cf = frag.casefold()
        out: list[Completion] = []
        for subcmd in sorted(_GIT_ALLOWLIST):
            if not frag or subcmd.casefold().startswith(frag_cf):
                out.append(
                    Completion(
                        f"@git:{subcmd}",
                        f"{prefix_text}@git:{subcmd}",
                        self.kind,
                        description=f"git {' '.join(_GIT_ALLOWLIST[subcmd][1:])}",
                    )
                )
        return out


@dataclass(slots=True, frozen=True)
class UrlMentionResolver:
    """``@url:<url>`` → no keystroke completions (freeform URL).

    Registers under kind ``"url"`` so the prefix is recognised by the routing
    layer rather than falling through to the file resolver.  Expansion to
    ``fetch <url> with web_fetch and use its content`` happens at submit time
    via ``expand_mentions``; no network call is made during completion
    (stays agent-mediated + SSRF-guarded).
    """

    kind: str = "url"

    def resolve(
        self, provider: CompletionProvider, prefix_text: str, frag: str, root: Path
    ) -> list[Completion]:
        # Freeform — no keystroke completions.
        return []


# ---------------------------------------------------------------------------
# Fixed read-only argv allowlist for @git: expansion
# ---------------------------------------------------------------------------

#: Subcommands exposed via ``@git:``; each maps to a fixed, read-only argv
#: (no shell, no user-supplied arguments).
_GIT_ALLOWLIST: dict[str, list[str]] = {
    "status": ["git", "status", "--porcelain=v1", "-b"],
    "diff": ["git", "diff"],
    "staged": ["git", "diff", "--staged"],
    "log": ["git", "log", "--oneline", "-15"],
}

_GIT_TIMEOUT: int = 5          # seconds
_GIT_MAX_CHARS: int = 2_000    # tail-truncate to this many chars

#: Regex matching ``@git:<subcmd>`` and ``@url:<rest>`` tokens; both are
#: whitespace-terminated (``\S+``) so they survive mid-sentence placement.
_MENTION_EXPAND_RE: re.Pattern[str] = re.compile(r"@(git|url):(\S+)")


def expand_mentions(text: str, project_root: Path | None = None) -> str:
    """Expand ``@git:X`` and ``@url:X`` tokens at submit time.

    ``@git:X`` — runs the fixed read-only argv for subcommand X, wraps the
    output in a ``<git-mention X (exit N)>…</git-mention>`` block, and passes
    the output through the central secret-redaction helper.  Unknown subcommands
    (not in ``_GIT_ALLOWLIST``) are left verbatim.  The subprocess is called
    directly (no shell), cwd=project_root, 5 s timeout, output tail-capped at
    2 000 chars.

    ``@url:https://…`` — pure text rewrite to
    ``fetch <url> with web_fetch and use its content``; no network call is made
    here (the agent performs the fetch via its own gated web_fetch tool).

    Tokens are whitespace-delimited (``@kind:\\S+``), consistent with the
    completion-engine tokenizer.  Multiple mentions in one message all expand
    in a single pass.  Expansion does not recurse into paste tokens or code
    fences already present in the text (expansion is a flat regex substitution;
    it will not match inside previously-expanded ``<git-mention>`` blocks
    because those blocks do not start with ``@``).
    """
    from jarn.config.secrets import redact_secrets  # local import to keep module light

    def _replace(m: re.Match[str]) -> str:
        kind = m.group(1)
        rest = m.group(2)

        if kind == "url":
            # Strip trailing punctuation from the URL before building the instruction,
            # then append it after so the user's sentence stays intact (e.g., "… content.").
            stripped_punct = ""
            while rest and rest[-1] in ".,;:!?)]>'\"":
                stripped_punct = rest[-1] + stripped_punct
                rest = rest[:-1]
            instruction = f"fetch {rest} with web_fetch and use its content"
            return instruction + stripped_punct

        # kind == "git"
        argv = _GIT_ALLOWLIST.get(rest)
        if argv is None:
            return m.group(0)  # unknown subcommand → left verbatim

        cwd = str(project_root) if project_root is not None else None
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                cwd=cwd,
            )
            exit_code = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
            if len(output) > _GIT_MAX_CHARS:
                output = output[-_GIT_MAX_CHARS:]
            output = redact_secrets(output)
            return f"<git-mention {rest} (exit {exit_code})>\n{output}</git-mention>"
        except subprocess.TimeoutExpired:
            return (
                f"<git-mention {rest} (error: timed out after {_GIT_TIMEOUT}s)>\n"
                f"git {rest}: timed out after {_GIT_TIMEOUT}s\n"
                "</git-mention>"
            )
        except Exception as exc:  # noqa: BLE001
            return (
                f"<git-mention {rest} (error: {exc})>\n"
                f"git {rest}: {exc}\n"
                "</git-mention>"
            )

    return _MENTION_EXPAND_RE.sub(_replace, text)


#: The default resolver for a bare ``@frag`` token.
_FILE_RESOLVER: FileMentionResolver = FileMentionResolver()

def _build_registry(*resolvers: MentionResolver) -> dict[str, MentionResolver]:
    """Index ``resolvers`` by their ``kind`` (keeps Protocol typing precise)."""
    return {r.kind: r for r in resolvers}


#: Registry of explicit ``@kind:`` mention resolvers, keyed by each resolver's
#: ``kind``.  Additive — new kinds register here without touching routing or
#: the bare-``@`` file path.
_RESOLVERS: dict[str, MentionResolver] = _build_registry(
    FolderMentionResolver(),
    SymbolMentionResolver(),
    GitMentionResolver(),
    UrlMentionResolver(),
)
