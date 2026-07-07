"""Post-edit diagnostics runner (T-3-3) — LSP-lite.

Runs ruff + pyright on the turn's edited files and returns structured
:class:`Diag` records so the agent can see and fix its own lint/type errors.

Design notes:
- Scope: only the turn's edited files (not the whole project), so pre-existing
  issues in unrelated files never appear.
- ruff: runs when ``shutil.which("ruff")`` is available.
- pyright: runs when ``shutil.which("pyright")`` AND the project looks like
  Python (``detect_capabilities`` finds pytest/ruff config, or a ``*.py``
  exists at the root).
- tsc: deliberately OFF by default (project-wide, slow); enable with
  ``verify.diagnostics_ts: true``.
- The 30 s combined timeout is enforced at the call site in session.py via
  ``asyncio.wait_for(asyncio.to_thread(collect_diagnostics, …), 30.0)``; each
  subprocess gets its own 28 s safety-net timeout so a stuck process cannot
  block the thread past the asyncio deadline.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

__all__ = ["Diag", "collect_diagnostics", "format_diagnostics"]


class Diag(NamedTuple):
    """A single diagnostic item from a lint/type tool."""

    file: str
    line: int
    severity: str   # "error" | "warning" | "information" | "hint"
    code: str
    message: str
    tool: str       # "ruff" | "pyright" | "tsc"


# ---------------------------------------------------------------------------
# ruff
# ---------------------------------------------------------------------------


def _run_ruff(paths: list[Path]) -> list[Diag]:
    """Run ``ruff check --output-format json`` on *paths*."""
    ruff = shutil.which("ruff")
    if not ruff:
        return []
    str_paths = [str(p) for p in paths if p and str(p)]
    if not str_paths:
        return []
    try:
        result = subprocess.run(
            [ruff, "check", "--output-format", "json", *str_paths],
            capture_output=True,
            text=True,
            timeout=28,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    try:
        items = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, ValueError):
        return []
    diags: list[Diag] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        loc = item.get("location") or {}
        diags.append(
            Diag(
                file=str(item.get("filename", "")),
                line=int(loc.get("row") or 0),
                severity="error",  # ruff doesn't categorise severity; treat all as error
                code=str(item.get("code", "")),
                message=str(item.get("message", "")),
                tool="ruff",
            )
        )
    return diags


# ---------------------------------------------------------------------------
# pyright
# ---------------------------------------------------------------------------


def _is_python_project(project_root: Path) -> bool:
    """Return True if *project_root* looks like a Python project."""
    from jarn.agent.verify import detect_capabilities

    caps = detect_capabilities(project_root)
    if caps.test or caps.lint:
        return True
    return any(project_root.glob("*.py"))


def _run_pyright(paths: list[Path], project_root: Path) -> list[Diag]:
    """Run ``pyright --outputjson`` on *paths* (Python projects only).

    Pyright reports 0-based line numbers; we normalise to 1-based.
    """
    pyright = shutil.which("pyright")
    if not pyright:
        return []
    if not _is_python_project(project_root):
        return []
    str_paths = [str(p) for p in paths if p and str(p)]
    if not str_paths:
        return []
    try:
        result = subprocess.run(
            [pyright, "--outputjson", *str_paths],
            capture_output=True,
            text=True,
            timeout=28,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    try:
        data = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return []
    diags: list[Diag] = []
    for item in data.get("generalDiagnostics") or []:
        if not isinstance(item, dict):
            continue
        loc = (item.get("range") or {}).get("start") or {}
        line = int(loc.get("line") or 0) + 1  # pyright 0-based → 1-based
        diags.append(
            Diag(
                file=str(item.get("file", "")),
                line=line,
                severity=str(item.get("severity", "error")),
                code=str(item.get("rule", "")),
                message=str(item.get("message", "")),
                tool="pyright",
            )
        )
    return diags


# ---------------------------------------------------------------------------
# tsc (opt-in via verify.diagnostics_ts — project-wide and slow by nature)
# ---------------------------------------------------------------------------

#: ``src/x.ts(12,5): error TS2322: Type 'string' is not assignable …``
_TSC_LINE_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),\d+\):\s+(?P<sev>error|warning)\s+"
    r"(?P<code>TS\d+):\s+(?P<msg>.*)$"
)


def _parse_tsc_output(
    output: str, paths: list[Path], project_root: Path
) -> list[Diag]:
    """Parse ``tsc --noEmit --pretty false`` output, keeping only *paths*.

    tsc checks the whole project (there is no per-file mode with a tsconfig),
    so pre-existing errors in files the turn did NOT edit are filtered out here
    to preserve the edited-files-only scope of the diagnostics loop.
    """
    # ``project_root / p`` is a no-op for an already-absolute p (pathlib rule),
    # so this normalises relative and absolute edited paths alike.
    edited = {str((project_root / p).resolve()) for p in paths}
    diags: list[Diag] = []
    for raw in output.splitlines():
        m = _TSC_LINE_RE.match(raw.strip())
        if not m:
            continue
        rel = m.group("file")
        abs_file = str((project_root / rel).resolve())
        if abs_file not in edited:
            continue
        diags.append(
            Diag(
                file=abs_file,
                line=int(m.group("line")),
                severity=m.group("sev"),
                code=m.group("code"),
                message=m.group("msg"),
                tool="tsc",
            )
        )
    return diags


def _run_tsc(paths: list[Path], project_root: Path) -> list[Diag]:
    """Run ``npx tsc --noEmit --pretty false`` (needs npx + tsconfig.json)."""
    npx = shutil.which("npx")
    if not npx:
        return []
    if not (project_root / "tsconfig.json").is_file():
        return []
    try:
        result = subprocess.run(
            [npx, "tsc", "--noEmit", "--pretty", "false"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=28,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _parse_tsc_output(result.stdout or "", paths, project_root)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_diagnostics(
    paths: list[Path], project_root: Path, *, ts: bool = False
) -> list[Diag]:
    """Run ruff + pyright (+ tsc when *ts*) on *paths*; return all :class:`Diag`s.

    Called via ``asyncio.to_thread`` from :mod:`jarn.agent.session` under a
    30-second ``asyncio.wait_for`` budget.  Each subprocess has its own 28 s
    timeout; a stuck process is abandoned and its results silently dropped.

    Scope is limited to *paths* (the turn's edited files) so pre-existing
    issues in untouched files never surface — including for tsc, whose
    project-wide output is filtered back down to *paths*.
    """
    diags: list[Diag] = []
    diags.extend(_run_ruff(paths))
    diags.extend(_run_pyright(paths, project_root))
    if ts:
        diags.extend(_run_tsc(paths, project_root))
    return diags


def format_diagnostics(diags: list[Diag], limit: int = 30) -> str:
    """Format *diags* as a human-readable string, capped at *limit* entries.

    Each line: ``  tool  file:line  severity  [code] message``.
    Over-limit items are summarised as ``  … (+N more)``.
    """
    shown = diags[:limit]
    lines: list[str] = []
    for d in shown:
        fname = Path(d.file).name if d.file else "?"
        loc = f"{fname}:{d.line}" if d.line else fname
        code = f"[{d.code}] " if d.code else ""
        lines.append(f"  {d.tool}  {loc}  {d.severity}  {code}{d.message}")
    if len(diags) > limit:
        lines.append(f"  … (+{len(diags) - limit} more)")
    return "\n".join(lines)
