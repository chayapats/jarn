"""Privacy-preserving ``jarn bug`` support handoff.

The diagnostic file is the same strict, allowlisted JSON report produced by
``jarn doctor --report``.  Raw doctor output and log lines are deliberately not
read: pattern redaction is useful defence in depth, but it cannot make arbitrary
user text, commands, or local paths safe to publish.

Without ``--dry-run`` the command shows a privacy preview and asks before opening
GitHub.  The pre-filled URL contains only a fixed issue template and the J.A.R.N.
version; the local report is never copied into the URL automatically.
"""

from __future__ import annotations

import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from jarn.version import __version__

_GITHUB_ISSUES_URL = "https://github.com/chayapats/jarn/issues/new"
_REPORT_FILENAME = "bug-report.json"

_ISSUE_BODY = """## What happened

Describe the problem and the last action you took.

## Expected behavior

Describe what you expected J.A.R.N. to do.

## Diagnostics

A privacy-scanned support report was generated locally. Review it before choosing
whether to attach it. The report is not included in this issue URL automatically.
"""


def _collect_doctor_diagnostics() -> dict[str, object]:
    """Collect diagnostics without serialising the unsafe full doctor payload."""

    # Import lazily so callers/tests can supply the same collector seam used by
    # ``jarn doctor`` without this module retaining a second implementation.
    import jarn.doctor.collect as doctor_collect

    diagnostics: dict[str, object] = {}
    doctor_collect.collect_doctor(diagnostics)
    return diagnostics


def build_report(
    home: Path,
    log_path: Path | None = None,
    *,
    known_secrets: set[str] | None = None,
) -> str:
    """Return the strict support-report JSON used by :func:`run_bug_report`.

    ``home`` and ``log_path`` remain accepted for source compatibility with the
    pre-GA helper. They are intentionally not read: neither filesystem paths nor
    log content belong in an automatically shareable support artifact.
    """

    del home, log_path
    from jarn.doctor.report import scan_support_report, support_report_json
    from jarn.errors import ErrorCode, JarnUserError, error_detail

    diagnostics = _collect_doctor_diagnostics()
    text = support_report_json(diagnostics, known_secrets=known_secrets)
    findings = scan_support_report(text, known_secrets=known_secrets)
    if findings:
        raise JarnUserError(
            error_detail(
                ErrorCode.DOCTOR_REPORT_FAILED,
                "Bug report failed its privacy scan.",
                cause=", ".join(findings),
                component="bug report",
                retryable=False,
                action="Do not share the report; run `jarn doctor` without --report.",
            )
        )
    return text


def _issue_url() -> str:
    """Build a URL containing no local diagnostic content."""

    title = f"Bug: J.A.R.N. {__version__}"
    return f"{_GITHUB_ISSUES_URL}?title={quote(title)}&body={quote(_ISSUE_BODY)}"


def _default_confirm_open(prompt: str) -> bool:
    answer = input(prompt).strip().lower()
    return answer in {"y", "yes"}


def _render_failure(exc: BaseException, *, report_path: Path | None = None) -> int:
    """Render one stable, centrally redacted failure and return non-success."""

    from jarn.config.secrets import redact_secrets
    from jarn.errors import ErrorCode, JarnUserError, error_detail

    if isinstance(exc, JarnUserError):
        detail = exc.detail
    else:
        detail = error_detail(
            ErrorCode.DOCTOR_REPORT_FAILED,
            "The privacy-scanned bug report could not be created.",
            cause=redact_secrets(str(exc)) or type(exc).__name__,
            component="bug report",
            retryable=True,
            action="Check the J.A.R.N. home permissions and retry `jarn bug --dry-run`.",
            report_path=report_path,
        )
    print(detail.render(), file=sys.stderr)
    return 1


def _render_cancelled() -> int:
    from jarn.errors import ErrorCode, error_detail

    print(
        error_detail(
            ErrorCode.CANCELLED,
            "GitHub issue handoff was cancelled.",
            cause="Consent to open a remote browser page was not given.",
            component="bug report",
            retryable=True,
            action=(
                "The local privacy-scanned report was kept; run `jarn bug` again "
                "when you want to open GitHub."
            ),
        ).render(),
        file=sys.stderr,
    )
    return 130


def _render_browser_failure(exc: BaseException | None = None) -> int:
    from jarn.config.secrets import redact_secrets
    from jarn.errors import ErrorCode, error_detail

    cause = (
        redact_secrets(str(exc))
        if exc is not None and str(exc)
        else "The operating system did not accept the browser-open request."
    )
    print(
        error_detail(
            ErrorCode.NETWORK_FAILED,
            "The GitHub issue form could not be opened.",
            cause=cause,
            component="bug report browser handoff",
            retryable=True,
            action=(
                "Open https://github.com/chayapats/jarn/issues/new manually; "
                "review the local report before attaching it."
            ),
        ).render(),
        file=sys.stderr,
    )
    return 6


def run_bug_report(
    *,
    dry_run: bool = False,
    home: Path | None = None,
    known_secrets: set[str] | None = None,
    confirm_open: Callable[[str], bool] | None = None,
) -> int:
    """Create a private report and optionally open a content-free issue form.

    The additive ``known_secrets`` and ``confirm_open`` seams let embedders add
    exact-value redaction and supply their own consent UI. Existing callers that
    pass only ``dry_run`` or ``home`` remain source-compatible.
    """

    from jarn.config import paths
    from jarn.doctor.report import write_support_report

    if home is None:
        home = paths.global_home()
    out_path = home / _REPORT_FILENAME

    try:
        diagnostics = _collect_doctor_diagnostics()
        written = write_support_report(
            diagnostics,
            out_path,
            known_secrets=known_secrets,
        )
    except Exception as exc:
        # This boundary guarantees no browser action follows any scan/write
        # failure. KeyboardInterrupt/SystemExit still unwind immediately, also
        # before the consent/browser code can run.
        return _render_failure(exc, report_path=out_path)

    print(f"Privacy-scanned bug report written to {written}")
    print(
        "Privacy preview: includes version/platform and diagnostic states/counts; "
        "excludes raw paths, logs, prompts, commands, file contents, and credentials."
    )
    print(
        "GitHub will receive only a blank issue template and the J.A.R.N. version. "
        "The report remains local unless you review and attach it yourself."
    )

    if dry_run:
        return 0

    confirm = confirm_open or _default_confirm_open
    try:
        allowed = bool(confirm("Open the GitHub issue form now? [y/N]: "))
    except (EOFError, KeyboardInterrupt):
        allowed = False
    if not allowed:
        return _render_cancelled()

    url = _issue_url()
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error) as exc:
        return _render_browser_failure(exc)
    if not opened:
        return _render_browser_failure()

    print("Opened the GitHub issue form; no local diagnostic content was placed in the URL.")
    return 0
