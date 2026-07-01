"""Grep-read per-project (and global) markdown knowledge base.

Pages live under ``<project>/.jarn/wiki/pages/*.md`` (project tier) and
``~/.jarn/wiki/pages/*.md`` (global tier). A one-line-per-page ``index.md``
at the root of each wiki dir acts as the cheap catalog injected into the
system prompt; full pages are read on demand.

No embeddings — just plain substring/grep search over the markdown files.
Git-friendly: each page is a standalone ``.md`` file, hand-editable, and
diffable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from jarn.config import paths
from jarn.memory.tokens import truncate_to_token_budget

#: Slugify: keep only letters, digits, hyphens, underscores. Max 80 chars.
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9_\-]+")

#: Characters that must never appear in a resolved path component.
_TRAVERSAL_RE = re.compile(r"\.\.")


def _slug(name: str) -> str:
    """Return a filesystem-safe slug from *name*.

    Lowercases, strips everything except ``[a-z0-9_-]``, and truncates at
    80 characters so page names never risk ridiculous filenames.
    """
    base = _SAFE_SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return base[:80] or "page"


def _sanitize_page_name(name: str) -> str:
    """Validate and return a safe slug for a wiki page name.

    Raises :exc:`ValueError` if *name* is empty, contains ``..``, contains
    path separators (``/`` or ``\\``), or resolves to an empty slug.  The
    returned value is the *slug* form — not the original name — so callers
    use it as both the display key and the filename stem.
    """
    if not name or not name.strip():
        raise ValueError("Page name must not be empty.")
    if _TRAVERSAL_RE.search(name):
        raise ValueError(
            f"Page name {name!r} is rejected: '..' is not allowed."
        )
    if "/" in name or "\\" in name:
        raise ValueError(
            f"Page name {name!r} is rejected: path separators are not allowed."
        )
    slug = _slug(name)
    if not slug or slug == "-":
        raise ValueError(
            f"Page name {name!r} produces an empty slug; use a different name."
        )
    return slug


@dataclass(slots=True)
class WikiStore:
    """A two-tier wiki store (global + optional project).

    ``project_wiki_dir`` points at ``<project>/.jarn/wiki/`` and may be
    ``None`` when no project root is discovered or wiki.enabled is False.
    ``global_wiki_dir`` points at ``~/.jarn/wiki/``.

    All public methods operate against the *combined* set of pages: project
    pages take precedence on name clashes when both tiers exist.
    """

    global_wiki_dir: Path
    project_wiki_dir: Path | None = field(default=None)

    # -- directory helpers ----------------------------------------------------

    @property
    def _global_pages_dir(self) -> Path:
        return self.global_wiki_dir / "pages"

    @property
    def _project_pages_dir(self) -> Path | None:
        return self.project_wiki_dir / "pages" if self.project_wiki_dir else None

    @property
    def _global_index(self) -> Path:
        return self.global_wiki_dir / "index.md"

    @property
    def _project_index(self) -> Path | None:
        return self.project_wiki_dir / "index.md" if self.project_wiki_dir else None

    # -- factory helpers ------------------------------------------------------

    @classmethod
    def build(
        cls,
        project_root: Path | None = None,
    ) -> WikiStore:
        """Return a :class:`WikiStore` from the discovered (or given) dirs.

        Project wiki dir is ``<project>/.jarn/wiki/``; global wiki dir is
        ``~/.jarn/wiki/``.
        """
        global_dir = paths.global_wiki_dir()
        pdir = paths.project_wiki_dir(project_root)
        return cls(global_wiki_dir=global_dir, project_wiki_dir=pdir)

    # -- public API -----------------------------------------------------------

    def search(self, query: str) -> list[tuple[str, list[str]]]:
        """Case-insensitive substring search across all pages.

        Returns a list of ``(page_slug, matching_lines)`` pairs, ordered
        project-tier first then global (both alphabetical within tier).
        Lines are returned as raw strings with the query match present.
        Pages with no match are omitted.
        """
        needle = query.lower()
        results: list[tuple[str, list[str]]] = []
        seen: set[str] = set()

        def _search_dir(pages_dir: Path) -> None:
            if not pages_dir.is_dir():
                return
            for page_file in sorted(pages_dir.glob("*.md")):
                slug = page_file.stem
                if slug in seen:
                    continue
                seen.add(slug)
                lines = page_file.read_text(encoding="utf-8").splitlines()
                matched = [line for line in lines if needle in line.lower()]
                if matched:
                    results.append((slug, matched))

        # Project pages first (highest priority), then global.
        pdir = self._project_pages_dir
        if pdir is not None:
            _search_dir(pdir)
        _search_dir(self._global_pages_dir)
        return results

    def read(self, page: str) -> str:
        """Return the raw markdown text of *page* (by slug or name).

        Project tier wins on name conflicts. Raises :exc:`FileNotFoundError`
        if the page does not exist in either tier.
        """
        slug = _sanitize_page_name(page)
        pdir = self._project_pages_dir
        if pdir is not None:
            candidate = pdir / f"{slug}.md"
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        candidate = self._global_pages_dir / f"{slug}.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Wiki page not found: {slug!r}")

    def write(
        self,
        page: str,
        content: str,
        *,
        tier: str = "project",
    ) -> str:
        """Create or overwrite *page* with *content*.

        ``tier`` is ``"project"`` (default) or ``"global"``.  When no project
        wiki dir is available, writes to the global tier regardless.

        Returns the relative path (e.g. ``"project:my-page.md"``) so the
        caller can surface it to the user.
        """
        slug = _sanitize_page_name(page)
        pages_dir = self._resolve_write_dir(tier)
        pages_dir.mkdir(parents=True, exist_ok=True)
        path = pages_dir / f"{slug}.md"
        path.write_text(content, encoding="utf-8")
        self._rebuild_index(pages_dir.parent)
        return f"{tier}:{slug}.md"

    def append(self, page: str, text: str, *, tier: str = "project") -> str:
        """Append *text* to an existing page (or create it if absent).

        The same tier-resolution logic as :meth:`write` applies.
        Returns the relative path.
        """
        slug = _sanitize_page_name(page)
        pages_dir = self._resolve_write_dir(tier)

        # Find the existing file to append to (either tier).
        existing: Path | None = None
        pdir = self._project_pages_dir
        if pdir is not None and (pdir / f"{slug}.md").is_file():
            existing = pdir / f"{slug}.md"
        elif (self._global_pages_dir / f"{slug}.md").is_file():
            existing = self._global_pages_dir / f"{slug}.md"

        if existing is not None:
            current = existing.read_text(encoding="utf-8")
            new_content = current.rstrip() + "\n\n" + text
            existing.write_text(new_content, encoding="utf-8")
            self._rebuild_index(existing.parent.parent)
            return f"{existing.parent.parent.name}:{slug}.md"

        # Page does not exist; create it in the target tier.
        pages_dir.mkdir(parents=True, exist_ok=True)
        path = pages_dir / f"{slug}.md"
        path.write_text(text, encoding="utf-8")
        self._rebuild_index(pages_dir.parent)
        return f"{tier}:{slug}.md"

    def index_text(self, *, token_budget: int | None = None) -> str:
        """Assemble the combined one-line catalog for system-prompt injection.

        Project pages are listed first (they override global on slug conflict).
        Returns an empty string when no pages exist in either tier.
        """
        lines: list[str] = []
        seen: set[str] = set()

        def _collect(pages_dir: Path, tier: str) -> None:
            if not pages_dir.is_dir():
                return
            for page_file in sorted(pages_dir.glob("*.md")):
                slug = page_file.stem
                if slug in seen:
                    continue
                seen.add(slug)
                summary = _extract_summary(page_file)
                lines.append(f"- [{slug}] ({tier}) — {summary}")

        pdir = self._project_pages_dir
        if pdir is not None:
            _collect(pdir, "project")
        _collect(self._global_pages_dir, "global")

        if not lines:
            return ""
        text = "# Wiki index\n\n" + "\n".join(lines) + "\n"
        if token_budget is not None:
            return truncate_to_token_budget(text, token_budget)
        return text

    # -- internals ------------------------------------------------------------

    def _resolve_write_dir(self, tier: str) -> Path:
        """Return the pages dir for *tier*, falling back to global."""
        if tier == "project" and self._project_pages_dir is not None:
            return self._project_pages_dir
        return self._global_pages_dir

    def _rebuild_index(self, wiki_dir: Path) -> None:
        """Rewrite the ``index.md`` in *wiki_dir* from current pages."""
        pages_dir = wiki_dir / "pages"
        if not pages_dir.is_dir():
            return
        lines = ["# Wiki index", ""]
        for page_file in sorted(pages_dir.glob("*.md")):
            slug = page_file.stem
            summary = _extract_summary(page_file)
            lines.append(f"- [{slug}]({slug}.md) — {summary}")
        text = "\n".join(lines) + "\n"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        (wiki_dir / "index.md").write_text(text, encoding="utf-8")


def _extract_summary(page_file: Path) -> str:
    """Return a one-line summary: first non-empty, non-heading line of the page."""
    try:
        for raw_line in page_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                # Strip markdown bold/italic markers for a cleaner summary.
                clean = re.sub(r"[*_`]", "", line)
                return clean[:120]
    except OSError:
        pass
    return page_file.stem
