"""Doc-sync: advertised pytest collection counts must match reality."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Files that state the *current* collection total (not historical sign-off tables).
CURRENT_COUNT_DOCS = (
    REPO / "README.md",
    REPO / "README-TH.md",
    REPO / "docs" / "CONTRIBUTING.md",
    REPO / "RELEASE.md",
)

# Match prose like "1320 tests", "**1320** tests", "1320 pytest cases".
_COUNT_RE = re.compile(
    r"(?:\*\*)?(\d{3,4})(?:\*\*)?\s+(?:pytest\s+)?tests?\b",
    re.IGNORECASE,
)


def _pytest_collection_count() -> int:
    proc = subprocess.run(
        ["uv", "run", "pytest", "--collect-only", "-q"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
    assert match, f"could not parse collection count from pytest output:\n{proc.stdout}"
    return int(match.group(1))


def _doc_counts(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8")
    if path.name == "RELEASE.md":
        # Only the live "Automated gates" block — sign-off tables are historical.
        text = text.split("## Manual QA", 1)[0]
    return [int(m.group(1)) for m in _COUNT_RE.finditer(text)]


@pytest.mark.parametrize("doc_path", CURRENT_COUNT_DOCS, ids=lambda p: p.name)
@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="collection count differs when POSIX-only tests are skipped on Windows",
)
def test_doc_test_count_matches_collection(doc_path: Path) -> None:
    expected = _pytest_collection_count()
    found = _doc_counts(doc_path)
    assert found, f"{doc_path} must mention the current pytest test count"
    assert all(n == expected for n in found), (
        f"{doc_path} advertises {found} but pytest collects {expected} tests"
    )
