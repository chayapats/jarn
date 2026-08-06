"""Shared parser for ``---`` YAML-frontmatter markdown files used by skills,
commands, and custom subagents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass(slots=True)
class FrontmatterDoc:
    meta: dict[str, Any]
    body: str
    path: Path


def parse(path: Path) -> FrontmatterDoc:
    """Parse a frontmatter markdown file. Missing frontmatter yields empty meta."""
    text = path.read_text(encoding="utf-8")
    m = _RE.match(text)
    if not m:
        return FrontmatterDoc(meta={}, body=text.strip(), path=path)
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return FrontmatterDoc(meta=meta, body=m.group(2).strip(), path=path)


def discover(
    dirs: list[Path],
    pattern: str | tuple[str, ...] = "*.md",
) -> list[Path]:
    """Return matching files across the given directories (skips missing dirs).

    ``pattern`` may be a single glob or a tuple of globs (applied in order
    within each directory). Later directories take precedence on name conflicts
    (caller decides); this just returns all paths in directory order, then
    pattern order, then filename order.
    """
    patterns = (pattern,) if isinstance(pattern, str) else pattern
    found: list[Path] = []
    for d in dirs:
        if d and d.is_dir():
            for pat in patterns:
                found.extend(sorted(p for p in d.glob(pat) if p.is_file()))
    return found
