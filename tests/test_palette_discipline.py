"""Fail CI if named Rich colors, [dim], [b]/[bold], or brand hex leak outside the SSOT."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "jarn"

_ALLOW_COLOR = {"palette.py"}
_ALLOW_BOLD = {"palette.py", "layout.py"}

_NAMED = re.compile(
    r"\[(?:/)?(?:(?:bold|b)\s+)?(?:green|red|yellow|cyan|blue|magenta)\b"
    r"|\[(?:/)?dim\]"
    r"|border_style\s*=\s*['\"](?:cyan|green|red|yellow|blue|magenta)['\"]"
    r"|style\s*=\s*['\"](?:(?:bold|reverse)\s+)*(?:green|red|yellow|cyan|blue|magenta|dim)\b"
)
_HEX = re.compile(
    r"#(?:22d3ee|7c8f94|3ee07a|3fb950|f85149|d29922)\b",
    re.IGNORECASE,
)
_BOLD = re.compile(r"\[(?:/)?(?:bold|b)(?:\s|\])")


def test_no_named_rich_colors_or_hardcoded_brand_hex_outside_palette() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if path.name not in _ALLOW_COLOR and (_NAMED.search(line) or _HEX.search(line)):
                offenders.append(f"{rel}:{i}:{line.strip()}")
            if path.name not in _ALLOW_BOLD and _BOLD.search(line):
                offenders.append(f"{rel}:{i}:{line.strip()}")
    assert not offenders, "Color/markup SSOT leak — use jarn.tui.palette / layout:\n" + "\n".join(
        offenders
    )
