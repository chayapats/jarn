"""Fail CI if named Rich colors or brand hex leak outside the palette SSOT."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "jarn"

# palette.py is the color table. Onboarding wizards are a separate first-run
# surface (wave E) and stay allowlisted until they share layout helpers.
_ALLOW_DIRS = ("onboarding",)
_ALLOW_FILES = {"palette.py"}

_NAMED = re.compile(r"\[(?:/)?(?:bold\s+)?(?:green|red|yellow|cyan|blue|magenta)\b")
_HEX = re.compile(
    r"#(?:22d3ee|7c8f94|3ee07a|3fb950|f85149|d29922)\b",
    re.IGNORECASE,
)


def _allowed(path: Path) -> bool:
    rel = path.relative_to(SRC)
    if rel.name in _ALLOW_FILES:
        return True
    return rel.parts[0] in _ALLOW_DIRS if rel.parts else False


def test_no_named_rich_colors_or_hardcoded_brand_hex_outside_palette() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if _allowed(path):
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _NAMED.search(line) or _HEX.search(line):
                offenders.append(f"{rel}:{i}:{line.strip()}")
    assert not offenders, "Color SSOT leak — use jarn.tui.palette / layout:\n" + "\n".join(
        offenders
    )
