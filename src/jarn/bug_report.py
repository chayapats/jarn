"""``jarn bug`` — assemble a redacted bug report and pre-fill a GitHub issue.

The report includes (all lines redacted via :func:`jarn.config.secrets.redact_secrets`):

- jarn version + platform + Python version
- ``jarn doctor --json`` output (redacted)
- Last 50 lines of ``~/.jarn/logs/jarn.log`` (redacted)

The file is written to ``~/.jarn/bug-report.md``.  Without ``--dry-run`` the
function also opens a pre-filled ``https://github.com/chayapats/jarn/issues/new``
URL (body ≤ 6 000 chars, HEAD+TAIL truncated; pointer to attach the full file).
"""

from __future__ import annotations

import platform
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote

from jarn.version import __version__

_GITHUB_ISSUES_URL = "https://github.com/chayapats/jarn/issues/new"
_REPORT_FILENAME = "bug-report.md"
_LOG_TAIL_LINES = 50
_BODY_MAX_CHARS = 6000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_doctor_json() -> str:
    """Return redacted doctor diagnostics as a JSON string.

    Imports ``collect_doctor`` lazily so monkeypatching in tests works — the
    function looks up the attribute on the module object at call time.
    """
    import jarn.doctor.collect as _dc
    from jarn.config.secrets import redact_secrets
    from jarn.doctor.render import doctor_to_json

    diag: dict = {}
    _dc.collect_doctor(diag)
    raw_json = doctor_to_json(diag)
    return redact_secrets(raw_json)


def _read_log_tail(log_path: Path) -> list[str]:
    """Return the last 50 lines of *log_path*, each passed through redact_secrets."""
    from jarn.config.secrets import redact_secrets

    if not log_path.is_file():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-_LOG_TAIL_LINES:]
    return [redact_secrets(line) for line in tail]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_report(home: Path, log_path: Path | None = None) -> str:
    """Build the full bug report as a Markdown string.

    Every included line passes through :func:`~jarn.config.secrets.redact_secrets`
    so no resolved secret value can appear in the output.

    Args:
        home:     The JARN home directory (``~/.jarn`` by default).
        log_path: Override the log file path (default: ``home/logs/jarn.log``).
    """
    from jarn.config.secrets import redact_secrets

    if log_path is None:
        log_path = home / "logs" / "jarn.log"

    # Header — each value individually redacted before embedding.
    version_line = redact_secrets(f"- **jarn version:** {__version__}")
    platform_line = redact_secrets(f"- **platform:** {platform.platform()}")
    python_line = redact_secrets(f"- **python:** {sys.version}")

    # Doctor diagnostics (collect + redact inside _collect_doctor_json).
    doctor_json = _collect_doctor_json()

    # Log tail — each line individually redacted.
    log_lines = _read_log_tail(log_path)
    log_section = "\n".join(log_lines) if log_lines else "(no log file found)"

    return "\n".join([
        "# jarn Bug Report",
        "",
        "## Environment",
        "",
        version_line,
        platform_line,
        python_line,
        "",
        "## Doctor Diagnostics",
        "",
        "```json",
        doctor_json,
        "```",
        "",
        f"## Last {_LOG_TAIL_LINES} Log Lines",
        "",
        "```",
        log_section,
        "```",
    ])


_ATTACH_NOTE = (
    "\n\n---\n"
    "📎 Please attach `~/.jarn/bug-report.md` for the full report."
)
_ELISION = (
    "\n\n[...truncated — attach `~/.jarn/bug-report.md` for the full report...]\n\n"
)


def _truncate_body(body: str) -> str:
    """HEAD+TAIL truncation to keep the GitHub issue body ≤ 6 000 chars.

    Keeps the beginning and end of the report; elides the middle.  Always
    appends a pointer to attach the full ``~/.jarn/bug-report.md`` file.
    """
    with_note = body + _ATTACH_NOTE
    if len(with_note) <= _BODY_MAX_CHARS:
        return with_note

    available = _BODY_MAX_CHARS - len(_ATTACH_NOTE) - len(_ELISION)
    if available < 0:
        # Pathological max — return just the attach note.
        return _ATTACH_NOTE[:_BODY_MAX_CHARS]
    head = max(0, available * 2 // 3)
    tail = max(0, available - head)
    return body[:head] + _ELISION + body[-tail:] + _ATTACH_NOTE


def run_bug_report(*, dry_run: bool = False, home: Path | None = None) -> int:
    """Write ``bug-report.md`` and (unless *dry_run*) open a prefilled issue URL.

    Args:
        dry_run: When ``True``, write the report file but do not open the browser.
        home:    Override the JARN home directory (for tests; defaults to
                 :func:`~jarn.config.paths.global_home`).

    Returns:
        0 on success.
    """
    from jarn.config import paths

    if home is None:
        home = paths.global_home()

    report = build_report(home)

    out_path = home / _REPORT_FILENAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    print(f"Bug report written to {out_path}")

    if dry_run:
        return 0

    title = f"Bug: jarn {__version__}"
    body_text = _truncate_body(report)
    url = f"{_GITHUB_ISSUES_URL}?title={quote(title)}&body={quote(body_text)}"

    print("Opening GitHub issue form…")
    webbrowser.open(url)
    return 0
