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

# Match prose like "1320 tests", "**1320** tests", "1320 pytest tests",
# "**1320** pytest cases", "1320 test cases", "2,151 tests".
#
# The "cases" spelling is NOT optional to support: JARN.md said `**1995** pytest
# cases` and the previous pattern — whose comment already claimed to match it —
# required the word "tests", so that line sat 156 behind while the gate reported the
# file clean off a second, current line in the same file.
#
# "cases" requires the "pytest" prefix. A bare "N cases" is ordinary English ("in 500
# cases") and matching it would fail the build on prose that has nothing to do with
# the suite; "N tests" and "N test cases" are not ambiguous that way.
#
# The count allows five and six digits and comma grouping, so passing 9,999 tests is
# not a silent hole.
_COUNT_RE = re.compile(
    r"(?:\*\*)?(\d{1,3}(?:,\d{3})+|\d{3,6})(?:\*\*)?\s+"
    r"(?:pytest\s+cases?|(?:pytest\s+)?tests?)\b",
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
    # Reuse the interpreter that is already executing this gate. Re-entering
    # through ``uv run`` makes collection depend on an external cache directory
    # (and on uv being installed), even though pytest and the project are already
    # loaded in the active environment. This stays hermetic in CI/sandboxes.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
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


#: In RELEASE.md the historical part starts at the first per-release sign-off
#: heading. Everything before it — "Automated gates", "Manual QA", "Publish",
#: "Post-release" — is live process that a reader follows today.
#:
#: The slice used to start at "## Manual QA", which discarded 135 lines including
#: three live sections. No count lives there today, so nothing was wrong — but a
#: count added to "Publish" tomorrow would have been invisible, which is precisely
#: the blind spot that let JARN.md sit 156 behind.
_RELEASE_HISTORY_RE = re.compile(r"^## v\d+\.\d+\.\d+ sign-off", re.MULTILINE)


def _doc_counts(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8")
    if path.name == "RELEASE.md":
        history = _RELEASE_HISTORY_RE.search(text)
        if history:
            text = text[: history.start()]
    return [int(m.group(1).replace(",", "")) for m in _COUNT_RE.finditer(text)]


def test_count_regex_matches_every_phrasing_the_docs_use() -> None:
    """The pattern is the gate's blind spot, so pin it directly.

    Discovering the right FILES is worth nothing if the pattern cannot see the
    sentence inside them: JARN.md advertised `**1995** pytest cases` while the gate
    read only its other, current line and called the file clean. This needs no
    collection subprocess, so it fails fast and locally.
    """
    def seen(text: str) -> list[int]:
        return [int(m.group(1).replace(",", "")) for m in _COUNT_RE.finditer(text)]

    # Every spelling that appears, or has appeared, in this repo's docs.
    assert seen("1320 tests") == [1320]
    assert seen("**1320** tests") == [1320]
    assert seen("currently **1320** tests)") == [1320]
    assert seen("1320 pytest tests") == [1320]
    assert seen("(**1995** pytest cases)") == [1995]      # JARN.md's spelling
    assert seen("1320 test cases") == [1320]
    assert seen("2,151 tests") == [2151]                  # comma grouping
    assert seen("12345 tests") == [12345]                 # past four digits

    # A bare "N cases" is ordinary English and must NOT fail a build.
    assert seen("resolved in 500 cases") == []
    # Too short to be a suite size — avoids matching version/section numbers.
    assert seen("12 tests") == []


def test_release_slice_keeps_the_live_sections_and_drops_the_sign_offs() -> None:
    """RELEASE.md is half live process, half frozen sign-off tables.

    The cut has to land between them: too late and a stale sign-off number fails the
    build forever, too early and a count in a live section is invisible. Asserted
    against the real file so a reorganisation of it cannot silently move the cut.
    """
    text = (REPO / "RELEASE.md").read_text(encoding="utf-8")
    history = _RELEASE_HISTORY_RE.search(text)
    assert history, "RELEASE.md must still carry per-release sign-off headings"
    live = text[: history.start()]

    # Every live section survives the cut…
    for heading in ("## Automated gates", "## Manual QA", "## Publish", "## Post-release"):
        assert heading in live, f"{heading} is live process and must stay in scope"
    # …and the frozen tables do not.
    assert "sign-off" not in live


def test_public_headless_exit_taxonomy_matches_contract_constants() -> None:
    """User-facing automation docs must not silently reuse legacy exit 1/2."""

    from jarn.exit_codes import (
        EXIT_BUDGET_EXCEEDED,
        EXIT_PERMISSION_DENIED,
        EXIT_TIMEOUT,
        EXIT_USAGE_CONFIG,
        EXIT_VERIFICATION_FAILED,
    )

    readme = (REPO / "README.md").read_text(encoding="utf-8")
    config_doc = (REPO / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    cli_source = (REPO / "src" / "jarn" / "cli.py").read_text(encoding="utf-8")

    assert f"`{EXIT_VERIFICATION_FAILED}` with `error.kind: \"schema\"`" in readme
    assert f"exit {EXIT_VERIFICATION_FAILED} with kind 'schema'" in cli_source
    for code in (
        EXIT_USAGE_CONFIG,
        EXIT_PERMISSION_DENIED,
        EXIT_BUDGET_EXCEEDED,
        EXIT_VERIFICATION_FAILED,
        EXIT_TIMEOUT,
    ):
        assert f"| `{code}` |" in config_doc


def test_every_emitted_stable_error_code_is_documented() -> None:
    """Adding a user-visible code without recovery documentation fails CI."""

    code_pattern = re.compile(r"JARN-[A-Z]+-[0-9]{3}")
    emitted: set[str] = set()
    for path in (REPO / "src" / "jarn").rglob("*.py"):
        emitted.update(code_pattern.findall(path.read_text(encoding="utf-8")))
    emitted.update(code_pattern.findall((REPO / "install.sh").read_text(encoding="utf-8")))

    reference = (REPO / "docs" / "ERROR_CODES.md").read_text(encoding="utf-8")
    documented = set(code_pattern.findall(reference))
    assert emitted <= documented, (
        "Stable error codes missing from docs/ERROR_CODES.md: "
        f"{sorted(emitted - documented)}"
    )


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
