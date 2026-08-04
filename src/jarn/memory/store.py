"""Markdown-first long-term memory.

Each memory is one human-readable markdown file with YAML frontmatter, stored
under ``~/.jarn/memory/`` (global) or ``<project>/.jarn/memory/`` (project). An
index file ``MEMORY.md`` lists one line per memory and is what gets injected
into the system prompt — keeping the prompt small while memories stay browsable
and editable by hand. Per-turn vector recall is layered on top of these files;
the markdown store remains the source of truth.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from jarn.config import paths
from jarn.memory.tokens import truncate_to_token_budget
from jarn.util.atomic import atomic_write_text, file_lock

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

MEMORY_TYPES = ("user", "feedback", "project", "reference")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")[:60] or "memory"


@dataclass(slots=True)
class Memory:
    name: str
    description: str
    body: str
    type: str = "project"
    path: Path | None = None

    def to_markdown(self) -> str:
        front = yaml.safe_dump(
            {"name": self.name, "description": self.description,
             "metadata": {"type": self.type}},
            sort_keys=False, allow_unicode=True,
        ).strip()
        return f"---\n{front}\n---\n\n{self.body.strip()}\n"


@dataclass(slots=True)
class MemoryStore:
    """Read/write markdown memories in one directory tier."""

    root: Path

    @classmethod
    def global_store(cls) -> MemoryStore:
        return cls(paths.global_memory_dir())

    @classmethod
    def project_store(cls, project_root: Path | None = None) -> MemoryStore | None:
        pdir = paths.project_dir(project_root)
        return cls(pdir / "memory") if pdir else None

    @property
    def index_path(self) -> Path:
        return self.root / "MEMORY.md"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, memory: Memory) -> Path:
        self.ensure()
        if memory.type not in MEMORY_TYPES:
            raise ValueError(f"Unknown memory type {memory.type!r}; expected {MEMORY_TYPES}")
        filename = f"{slugify(memory.name)}.md"
        path = self.root / filename
        atomic_write_text(path, memory.to_markdown())
        memory.path = path
        self._rebuild_index()
        self._invalidate_vector_cache()
        return path

    def get(self, name: str) -> Memory | None:
        """Return a memory by exact name or slug."""
        wanted_name = name.strip().lower()
        wanted_slug = slugify(name)
        for memory in self.load_all():
            if (
                memory.name.strip().lower() == wanted_name
                or slugify(memory.name) == wanted_slug
            ):
                return memory
        return None

    def delete(self, name: str) -> bool:
        """Delete a memory by exact name or slug and rebuild the index."""
        memory = self.get(name)
        if memory is None or memory.path is None:
            return False
        with contextlib.suppress(FileNotFoundError):
            memory.path.unlink()
        self._rebuild_index()
        self._invalidate_vector_cache()
        return True

    def load_all(self) -> list[Memory]:
        if not self.root.is_dir():
            return []
        out: list[Memory] = []
        for path in sorted(self.root.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            mem = self._parse(path)
            if mem:
                out.append(mem)
        return out

    def index_text(self, *, token_budget: int | None = None) -> str:
        """Return the MEMORY.md index (created on demand) for prompt injection."""
        if self.index_path.is_file():
            text = self.index_path.read_text(encoding="utf-8")
        else:
            text = self._rebuild_index()
        if token_budget is not None:
            return truncate_to_token_budget(text, token_budget)
        return text

    # -- internals ----------------------------------------------------------

    def _parse(self, path: Path) -> Memory | None:
        text = path.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return None
        try:
            front = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            return None
        meta = front.get("metadata", {}) or {}
        return Memory(
            name=str(front.get("name", path.stem)),
            description=str(front.get("description", "")),
            body=m.group(2).strip(),
            type=str(meta.get("type", "project")),
            path=path,
        )

    def _rebuild_index(self) -> str:
        if not self.root.is_dir():
            # Nothing to index and no directory yet — materialise NOTHING. This must
            # precede file_lock, whose mkdir would create ``self.root`` (and a
            # MEMORY.md.lock inside it) and make the write guard below vacuously
            # true. index_text() reaches here on the READ path, i.e. on every
            # system-prompt build, where creating a store in the user's repo from a
            # pure read is exactly what the original guard existed to prevent.
            return "# Memory index\n\n"
        # Locked and published atomically. This runs on the READ path too —
        # `index_text()` rebuilds on demand, and that text goes straight into the
        # system prompt — so a bare write_text let a concurrent prompt assembly
        # read a half-written or empty MEMORY.md.
        with file_lock(self.index_path):
            memories = self.load_all()
            lines = ["# Memory index", ""]
            for mem in memories:
                lines.append(
                    f"- [{mem.name}]({slugify(mem.name)}.md) — {mem.description}"
                )
            text = "\n".join(lines) + "\n"
            if memories or self.root.is_dir():
                self.ensure()
                atomic_write_text(self.index_path, text)
        return text

    def _invalidate_vector_cache(self) -> None:
        with contextlib.suppress(OSError):
            (self.root / ".vectors.json").unlink()
