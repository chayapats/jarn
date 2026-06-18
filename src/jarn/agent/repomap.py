"""Ranked, token-budgeted repository map.

Builds a compact, markdown-ish overview of a codebase so the agent can orient
quickly on large repos without reading every file. Symbols are extracted with:

  - Python (.py): stdlib ``ast`` — accurate, no deps.
  - JS/TS/Go/Rust: lightweight regex for top-level declarations (imperfect but
    good enough for a map — this is not a parser).
  - Everything else: skipped (and binaries, huge files, and noise dirs).

Ranking heuristic (deterministic, no LLM):
  score = symbols_count + xref_count + depth_bonus

  * ``symbols_count``  — number of top-level symbols defined in the file.
    More symbols → more important (main logic files beat stubs).
  * ``xref_count``     — how many other source files reference this file's
    stem (name without extension).  A file imported/used by many siblings
    ranks higher than isolated utilities.
  * ``depth_bonus``    — shallower paths score higher: ``max_depth - depth``
    where depth is the number of path components below the project root.
    Root-level files like ``main.py`` or ``app.go`` naturally rank first.
  * ``focus`` (optional) — if the caller passes a focus string, files whose
    path contains that string receive a bonus of 10 to bias the top of the map.
  * Ties are broken by alphabetical path (determinism guarantee).

Caching:
  The computed map is cached under ``paths.cachedir() / "repomap"`` as a JSON
  file keyed by ``root`` + a cheap signature (max mtime + file count).  Cache
  read/write never raises — it is best-effort.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jarn.config import paths

logger = logging.getLogger("jarn.repomap")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Extensions whose symbols we extract.
_PY_EXTS: frozenset[str] = frozenset({".py"})
_JS_TS_EXTS: frozenset[str] = frozenset({".js", ".jsx", ".ts", ".tsx"})
_GO_EXTS: frozenset[str] = frozenset({".go"})
_RUST_EXTS: frozenset[str] = frozenset({".rs"})

_SUPPORTED_EXTS: frozenset[str] = _PY_EXTS | _JS_TS_EXTS | _GO_EXTS | _RUST_EXTS

#: Directories to skip when not using git ls-files.
_NOISE_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".jarn", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".tox", ".eggs", "*.egg-info",
})

#: Soft file-size cap — skip files over this (avoids reading minified bundles).
_MAX_FILE_BYTES: int = 512_000

#: Token estimation fallback: 4 chars ≈ 1 token.
_CHARS_PER_TOKEN: int = 4

# ---------------------------------------------------------------------------
# Regex patterns for non-Python languages (JS/TS/Go/Rust)
# ---------------------------------------------------------------------------

#: JS/TS top-level declarations.
_JSTS_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
    r"|^export\s+(?:default\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"|^(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)

#: Go top-level declarations: functions, types, and vars.
_GO_RE = re.compile(
    r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_]\w*)"
    r"|^type\s+([A-Za-z_]\w*)"
    r"|^var\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)

#: Rust top-level declarations.
_RUST_RE = re.compile(
    r"^(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"
    r"|^(?:pub\s+)?struct\s+([A-Za-z_]\w*)"
    r"|^(?:pub\s+)?enum\s+([A-Za-z_]\w*)"
    r"|^(?:pub\s+)?trait\s+([A-Za-z_]\w*)"
    r"|^(?:pub\s+)?type\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FileEntry:
    """One file's extracted symbols and computed rank score."""

    path: Path                            # absolute path
    rel: str                              # relative path string (from root)
    symbols: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass(slots=True, frozen=True)
class SymbolRef:
    """A single named symbol located in the repo, for @symbol completion.

    ``container`` is the enclosing class name for a method (one level deep,
    matching :func:`_extract_python`); it is empty for top-level symbols.
    """

    name: str
    rel: str                              # relative path string (from root, posix)
    container: str = ""


def _extract_python(text: str) -> list[str]:
    """Extract module-level class/def/async-def names (and class methods one
    level deep) using ``ast``.  Robust and immune to regex edge cases."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
            if isinstance(node, ast.ClassDef):
                # One level deep: class methods.
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.append(f"  .{child.name}")
    return names


def _extract_jsts(text: str) -> list[str]:
    names: list[str] = []
    for m in _JSTS_RE.finditer(text):
        name = m.group(1) or m.group(2) or m.group(3)
        if name:
            names.append(name)
    return names


def _extract_go(text: str) -> list[str]:
    names: list[str] = []
    for m in _GO_RE.finditer(text):
        name = m.group(1) or m.group(2) or m.group(3)
        if name:
            names.append(name)
    return names


def _extract_rust(text: str) -> list[str]:
    names: list[str] = []
    for m in _RUST_RE.finditer(text):
        name = next((g for g in m.groups() if g), None)
        if name:
            names.append(name)
    return names


def _extract_symbols(path: Path) -> list[str]:
    """Read *path* and return a list of symbol names, or [] on any error."""
    ext = path.suffix.lower()
    if ext not in _SUPPORTED_EXTS:
        return []
    try:
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if ext in _PY_EXTS:
        return _extract_python(text)
    if ext in _JS_TS_EXTS:
        return _extract_jsts(text)
    if ext in _GO_EXTS:
        return _extract_go(text)
    if ext in _RUST_EXTS:
        return _extract_rust(text)
    return []


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _is_noise(part: str) -> bool:
    """True if a path component is a well-known noise directory."""
    return part in _NOISE_DIRS or part.endswith(".egg-info")


def _discover_files(root: Path) -> list[Path]:
    """Return source files under *root*, respecting .gitignore when possible.

    When *root* is a git repo ``git ls-files`` is used so .gitignore is
    honoured for free.  Otherwise we walk the tree and skip noise dirs.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            files: list[Path] = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                p = root / line
                if p.suffix.lower() in _SUPPORTED_EXTS and p.is_file():
                    files.append(p)
            return files
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Fallback: manual walk skipping noise dirs.
    files = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(_is_noise(part) for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() in _SUPPORTED_EXTS:
            files.append(p)
    return files


# ---------------------------------------------------------------------------
# Cross-reference counting
# ---------------------------------------------------------------------------

def _build_xref_counts(entries: list[FileEntry]) -> dict[str, int]:
    """Count how many files reference each file's stem.

    A "reference" is a case-sensitive substring match of the stem (e.g.
    ``repomap``) in another file's relative path.  This is intentionally
    cheap — we are not parsing imports, just counting name appearances in
    sibling paths, which correlates well with actual import graphs for
    Python packages and Go modules.
    """
    stems = {e.path.stem for e in entries}
    counts: dict[str, int] = {s: 0 for s in stems}
    for entry in entries:
        for stem in stems:
            if stem == entry.path.stem:
                continue
            if stem in entry.rel:
                counts[stem] += 1
    return counts


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def _rank(entries: list[FileEntry], root: Path, focus: str = "") -> list[FileEntry]:
    """Assign ``score`` to each entry (higher = more important) and sort.

    Scoring (all additive):
      ``symbols_count``  — number of top-level symbols (promotes logic-heavy files).
      ``xref_count``     — number of other files that reference this file's stem.
      ``depth_bonus``    — ``max_depth - depth`` so shallower paths score higher.
      ``focus_bonus``    — +10 when the file's rel path contains the focus string.
    Ties broken by ``rel`` path (alphabetical) for determinism.
    """
    if not entries:
        return entries

    xrefs = _build_xref_counts(entries)
    depths = [len(Path(e.rel).parts) for e in entries]
    max_depth = max(depths) if depths else 1

    for entry, depth in zip(entries, depths, strict=True):
        sym_count = sum(1 for s in entry.symbols if not s.startswith("  ."))
        xref = xrefs.get(entry.path.stem, 0)
        depth_bonus = max_depth - depth
        focus_bonus = 10.0 if focus and focus in entry.rel else 0.0
        entry.score = sym_count + xref + depth_bonus + focus_bonus

    return sorted(entries, key=lambda e: (-e.score, e.rel))


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

def _count_tokens(text: str) -> int:
    """Count tokens in *text*, falling back to len/4 if tiktoken fails."""
    try:
        import tiktoken  # already a project dependency

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001
        return max(1, len(text) // _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

#: Module-level in-process cache for the symbol index, keyed by
#: ``_cache_key(root, _cheap_signature(files))``.  The REPL completer builds a
#: fresh ``CompletionProvider`` on EVERY keystroke, so the symbol index MUST be
#: cached here (process-global), NOT on the provider, or a large repo would be
#: re-scanned per character and stall the UI.  Same cheap signature (file count
#: + max mtime) as the on-disk repo-map cache — edits within the same mtime
#: second can be missed, which is acceptable for an authoring aid.
_SYMBOL_INDEX_CACHE: dict[str, list[SymbolRef]] = {}

#: Short-TTL cache of ``(files, signature)`` per root. The REPL builds a fresh
#: completer on EVERY keystroke; without this, the symbol completer re-forks
#: ``git ls-files`` and re-stats every file on each character even on a
#: symbol-index cache hit, because the signature it keys on was recomputed every
#: call (the O(repo) the index cache was meant to avoid). 1s TTL — same
#: same-second staleness tradeoff as the dir-listing / symbol-index caches.
_DISCOVERY_TTL = 1.0
_DISCOVERY_CACHE: dict[str, tuple[float, list[Path], str]] = {}


def _discover_with_signature(root: Path) -> tuple[list[Path], str]:
    """``(_discover_files(root), _cheap_signature(files))`` behind a short-TTL
    process cache so the per-keystroke symbol completer doesn't fork git + stat
    every file on each character."""
    key = str(root)
    now = time.monotonic()
    cached = _DISCOVERY_CACHE.get(key)
    if cached is not None and now - cached[0] < _DISCOVERY_TTL:
        return cached[1], cached[2]
    files = _discover_files(root)
    signature = _cheap_signature(files)
    _DISCOVERY_CACHE[key] = (now, files, signature)
    return files, signature


def _cache_key(root: Path, signature: str) -> str:
    h = hashlib.sha256(f"{root}:{signature}".encode()).hexdigest()[:16]
    return h


def _cache_path(root: Path, signature: str) -> Path:
    return paths.cachedir() / "repomap" / f"{_cache_key(root, signature)}.json"


def _cheap_signature(files: list[Path]) -> str:
    """A cheap cache-invalidation key: file count + max mtime."""
    if not files:
        return "empty:0"
    try:
        max_mtime = max(p.stat().st_mtime for p in files)
        return f"{len(files)}:{max_mtime:.3f}"
    except OSError:
        return f"{len(files)}:unknown"


def _load_cache(path: Path) -> str | None:
    """Return cached map text, or None on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("map", ""))
    except Exception:  # noqa: BLE001
        return None


def _save_cache(path: Path, map_text: str) -> None:
    """Write map text to cache; silently swallow any error."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"map": map_text}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_repo_map(
    root: Path,
    *,
    token_budget: int,
    focus: str = "",
) -> str:
    """Build and return a ranked, token-budgeted map of the repository at *root*.

    The map is a human-readable markdown-ish text listing files ordered by
    importance (see module docstring for the ranking heuristic), each with its
    top-level symbols.  The output is truncated to fit ``token_budget`` tokens.

    ``focus`` (optional) biases ranking toward files whose path contains the
    string (case-sensitive substring match), receiving a +10 score bonus.

    The result is cached under ``paths.cachedir()``; the cache is invalidated
    when the file count or the maximum mtime of discovered files changes.
    Cache I/O never raises.
    """
    files = _discover_files(root)
    signature = _cheap_signature(files)
    cache_key_focus = f"{focus}:{signature}"
    cp = _cache_path(root, cache_key_focus)
    cached = _load_cache(cp)
    if cached is not None:
        return cached

    entries = _build_entries(root, files)
    ranked = _rank(entries, root, focus=focus)
    map_text = _render(root, ranked, token_budget=token_budget)
    _save_cache(cp, map_text)
    return map_text


def build_symbol_index(root: Path) -> list[SymbolRef]:
    """Return a flat list of :class:`SymbolRef` for every symbol under *root*.

    Reuses :func:`_discover_files` (git-ls-files / .gitignore-aware, noise-dir
    filtered) and :func:`_extract_symbols` (Python via ``ast`` including
    one-level methods; JS/TS/Go/Rust via regex), so language coverage matches
    :func:`build_repo_map`.  Python method entries arrive as ``"  .name"`` and
    are normalized into ``name=method, container=<enclosing class>``.

    The result is cached in the process-global :data:`_SYMBOL_INDEX_CACHE`
    keyed by ``root`` + a cheap signature (file count + max mtime). File
    discovery + the signature themselves go through :func:`_discover_with_signature`
    (a 1s-TTL cache), so the per-keystroke REPL completer does a genuine
    O(prefix-match) lookup with no ``git ls-files`` fork or per-file ``stat`` on a
    cache hit.  See the cache docstrings for the staleness tradeoff.
    """
    files, signature = _discover_with_signature(root)
    key = _cache_key(root, signature)
    cached = _SYMBOL_INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    refs: list[SymbolRef] = []
    for p in files:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = str(p)
        container = ""
        for sym in _extract_symbols(p):
            if sym.startswith("  ."):
                # A class method (one level deep); attach to the last class.
                refs.append(SymbolRef(name=sym[3:], rel=rel, container=container))
            else:
                container = sym  # a subsequent "  .method" belongs to this name
                refs.append(SymbolRef(name=sym, rel=rel, container=""))
    _SYMBOL_INDEX_CACHE[key] = refs
    return refs


def _build_entries(root: Path, files: list[Path]) -> list[FileEntry]:
    """Build FileEntry objects (with symbols extracted) for all *files*."""
    entries: list[FileEntry] = []
    for p in files:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = str(p)
        syms = _extract_symbols(p)
        entries.append(FileEntry(path=p, rel=rel, symbols=syms))
    return entries


def _render(root: Path, ranked: Sequence[FileEntry], token_budget: int) -> str:
    """Render ranked entries into a markdown-ish map, truncated to *token_budget*."""
    # resolve() so Path('.').name (empty string) shows the real dir name.
    header = f"# Repo map — {root.resolve().name}\n\n"
    lines: list[str] = [header]
    used = _count_tokens(header)

    for entry in ranked:
        block = _entry_block(entry)
        cost = _count_tokens(block)
        if used + cost > token_budget:
            # Try a compact version (path only, no symbols).
            compact = f"- `{entry.rel}`\n"
            c2 = _count_tokens(compact)
            if used + c2 > token_budget:
                break
            lines.append(compact)
            used += c2
        else:
            lines.append(block)
            used += cost

    return "".join(lines)


def _entry_block(entry: FileEntry) -> str:
    """Format a single file entry."""
    if not entry.symbols:
        return f"- `{entry.rel}`\n"
    sym_lines = "\n".join(f"  - `{s}`" for s in entry.symbols)
    return f"- `{entry.rel}`\n{sym_lines}\n"
