#!/usr/bin/env python3
"""Fail CI when a new unreviewed shell-execution surface enters ``src/jarn``.

Ordinary subprocess integrations must pass argv with the default ``shell=False``.
The few intentional shell surfaces are security boundaries and carry an explicit
``security: reviewed-shell=...`` annotation at the call site. This audit does not
replace runtime permission tests; it prevents a new bypass from landing unnoticed.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path

REVIEW_MARKER = "security: reviewed-shell="
APPROVED_REASONS = {"permission-engine", "trusted-project-hook"}
SUBPROCESS_CALLS = {
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "subprocess.Popen",
    "subprocess.run",
}
FORBIDDEN_CALLS = {
    "asyncio.create_subprocess_shell",
    "os.popen",
    "os.system",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str
    message: str


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _shell_keyword(call: ast.Call) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == "shell"), None)


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _review_reason(lines: list[str], line: int) -> str | None:
    # Allow the marker on the call, its argument, or the immediately preceding
    # explanation while keeping the approval physically bound to this sink.
    for candidate in lines[max(0, line - 3) : line + 2]:
        if REVIEW_MARKER in candidate:
            return candidate.split(REVIEW_MARKER, 1)[1].strip().split()[0]
    return None


def audit_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        relative = str(path.relative_to(root.parent))
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            findings.append(Finding(relative, exc.lineno or 1, "syntax", str(exc)))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _qualified_name(node.func)
            if name in FORBIDDEN_CALLS:
                findings.append(
                    Finding(relative, node.lineno, "forbidden-shell-api", f"use argv, not {name}")
                )
                continue
            if name not in SUBPROCESS_CALLS:
                continue
            shell_value = _shell_keyword(node)
            if shell_value is not None and not isinstance(shell_value, ast.Constant):
                findings.append(
                    Finding(
                        relative,
                        node.lineno,
                        "dynamic-shell-flag",
                        "shell must be a literal false value or an explicitly reviewed true value",
                    )
                )
            if not _is_true(shell_value):
                continue
            reason = _review_reason(lines, node.lineno)
            if reason not in APPROVED_REASONS:
                findings.append(
                    Finding(
                        relative,
                        node.lineno,
                        "unreviewed-shell-true",
                        f"shell=True requires {REVIEW_MARKER}<approved-reason>",
                    )
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("src/jarn"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    findings = audit_tree(args.root)
    if args.json:
        print(json.dumps({"schemaVersion": 1, "ok": not findings, "findings": [asdict(f) for f in findings]}))
    elif findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.code}: {finding.message}")
    else:
        print("Subprocess security audit passed.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
