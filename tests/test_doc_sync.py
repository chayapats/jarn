"""Doc-sync: advertised pytest collection counts must match reality.

The count is stated in eight places, and the gate used to name four of them. The
other four drifted exactly as you would expect a hand-maintained list to let them:
``SPEC.md`` sat 469 tests behind while literally calling itself *"Gate ปัจจุบัน"*,
and ``JARN.md`` 154 behind under "How to run / test".

So the list is no longer the gate's source of truth. Every tracked ``*.md`` is
discovered; anything stating a count must state the right one unless it is an
explicit historical record. Four files must state it at all, since they are where
a reader looks.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Files that MUST state the current collection total — a reader looks here, so a
# silent removal is a regression too.
CURRENT_COUNT_DOCS = (
    REPO / "README.md",
    REPO / "README-TH.md",
    REPO / "docs" / "CONTRIBUTING.md",
    REPO / "RELEASE.md",
)

#: Docs allowed to carry a count that is NOT the current one, because they record a
#: moment rather than the present gate. Keep this list short and justified: every
#: entry is a file that can drift without anyone noticing.
HISTORICAL_COUNT_DOCS = frozenset({
    "CHANGELOG.md",                  # per-release notes; each entry is a snapshot
    "PROJECT_AUDIT_2026-06-08.md",   # dated audit, deliberately frozen
})

# Match prose like "1320 tests", "**1320** tests", "1320 pytest cases".
_COUNT_RE = re.compile(
    r"(?:\*\*)?(\d{3,4})(?:\*\*)?\s+(?:pytest\s+)?tests?\b",
    re.IGNORECASE,
)

_skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="collection count differs when POSIX-only tests are skipped on Windows",
)


@functools.cache
def _pytest_collection_count() -> int:
    """Collect once per session — this shells out to a full pytest collection, and
    the discovery test below would otherwise pay for it on every file."""
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


def _tracked_markdown() -> list[Path]:
    """Every tracked ``*.md`` in the repo. Uses git rather than a glob so untracked
    scratch notes and vendored trees cannot fail the build."""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO / name for name in proc.stdout.split("\0") if name]


def _doc_counts(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8")
    if path.name == "RELEASE.md":
        # Only the live "Automated gates" block — sign-off tables are historical.
        text = text.split("## Manual QA", 1)[0]
    return [int(m.group(1)) for m in _COUNT_RE.finditer(text)]


@pytest.mark.parametrize("doc_path", CURRENT_COUNT_DOCS, ids=lambda p: p.name)
@_skip_on_windows
def test_doc_test_count_matches_collection(doc_path: Path) -> None:
    expected = _pytest_collection_count()
    found = _doc_counts(doc_path)
    assert found, f"{doc_path} must mention the current pytest test count"
    assert all(n == expected for n in found), (
        f"{doc_path} advertises {found} but pytest collects {expected} tests"
    )


@_skip_on_windows
def test_no_tracked_doc_states_a_stale_test_count() -> None:
    """Any doc that mentions a test count must mention the right one.

    This is the half the named list could not do: a file that starts quoting the
    count — or one that already did and was never added to the list — is caught
    without anyone having to remember it exists. To exempt a genuine record, add it
    to ``HISTORICAL_COUNT_DOCS`` with a reason.
    """
    expected = _pytest_collection_count()
    stale: dict[str, list[int]] = {}
    for path in _tracked_markdown():
        if path.name in HISTORICAL_COUNT_DOCS:
            continue
        wrong = [n for n in _doc_counts(path) if n != expected]
        if wrong:
            stale[str(path.relative_to(REPO))] = wrong

    assert not stale, (
        f"pytest collects {expected} tests, but these docs advertise otherwise: "
        f"{stale}. Update them, or add a genuine historical record to "
        f"HISTORICAL_COUNT_DOCS with a reason."
    )
