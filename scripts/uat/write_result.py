#!/usr/bin/env python3
"""Write one bounded, redacted, atomic UAT evidence document."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|sess|key)-[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
)
_CANDIDATE_VERSION_PATTERN = re.compile(r"^[0-9][0-9A-Za-z.!+_-]*$")
_CANDIDATE_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROJECT_VERSION_PATTERN = re.compile(
    r'^version\s*=\s*"([^"]+)"\s*$',
    re.MULTILINE,
)


def _redact(value: str, *, home: str = "", host: str = "") -> str:
    text = value
    if home:
        text = text.replace(home, "$HOME")
    if host:
        text = text.replace(host, "[HOST]")
    text = _SECRET_PATTERNS[0].sub("[REDACTED_SECRET]", text)
    text = _SECRET_PATTERNS[1].sub(r"\1[REDACTED_SECRET]", text)
    text = re.sub(
        r"(?i)\b(token|password|secret|api[_-]?key|device[_-]?code)"
        r"([=:]\s*|\s+)[^\s,;]+",
        r"\1\2[REDACTED_SECRET]",
        text,
    )
    return text[:4000]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--candidate-version",
        help=(
            "Exact candidate version; defaults to JARN_UAT_CANDIDATE_VERSION "
            "or the repository pyproject.toml"
        ),
    )
    parser.add_argument(
        "--candidate-commit",
        help=(
            "Optional full candidate Git commit; defaults to "
            "JARN_UAT_CANDIDATE_COMMIT when set"
        ),
    )
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--uat-id")
    identity.add_argument("--record-id")
    parser.add_argument(
        "--criterion-id",
        action="append",
        default=[],
        help="Criterion supported by this result; repeatable (defaults to --uat-id)",
    )
    parser.add_argument(
        "--status", choices=("passed", "failed", "blocked", "not_run"), required=True
    )
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--ended-at", required=True)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--command", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--implementation", action="append", default=[])
    parser.add_argument("--automated-test", action="append", default=[])
    parser.add_argument("--decision", action="append", default=[])
    parser.add_argument("--error", action="append", default=[])
    parser.add_argument("--documentation-lookup", action="append", default=[])
    parser.add_argument("--limitation", action="append", default=[])
    parser.add_argument("--platform-os", default="unknown")
    parser.add_argument("--platform-version", default="unknown")
    parser.add_argument("--platform-arch", default="unknown")
    parser.add_argument("--platform-libc", default="unknown")
    parser.add_argument("--redact-home", default="")
    parser.add_argument("--redact-host", default="")
    return parser


def _candidate_version(explicit: str | None) -> str:
    value = explicit or os.environ.get("JARN_UAT_CANDIDATE_VERSION")
    if value is None:
        try:
            project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(
                f"error: cannot derive candidate version from pyproject.toml: {exc}"
            ) from exc
        match = _PROJECT_VERSION_PATTERN.search(project)
        if match is None:
            raise SystemExit("error: cannot derive candidate version from pyproject.toml")
        value = match.group(1)
    if _CANDIDATE_VERSION_PATTERN.fullmatch(value) is None:
        raise SystemExit("error: invalid candidate version")
    return value


def _candidate_commit(explicit: str | None) -> str | None:
    value = explicit or os.environ.get("JARN_UAT_CANDIDATE_COMMIT")
    if value is None or value == "":
        return None
    normalized = value.lower()
    if _CANDIDATE_COMMIT_PATTERN.fullmatch(normalized) is None:
        raise SystemExit(
            "error: candidate commit must be a full 40- or 64-character hexadecimal ID"
        )
    return normalized


def _redacted_list(values: list[str], args: argparse.Namespace) -> list[str]:
    return [_redact(value, home=args.redact_home, host=args.redact_host) for value in values]


def build_result(args: argparse.Namespace) -> dict[str, Any]:
    record_id = args.record_id or args.uat_id
    criterion_ids = args.criterion_id or ([args.uat_id] if args.uat_id else [])
    result = {
        "schema_version": 2,
        "record_id": record_id,
        "candidate_version": _candidate_version(args.candidate_version),
        "criterion_ids": criterion_ids,
        "status": args.status,
        "started_at": args.started_at,
        "ended_at": args.ended_at,
        "duration_seconds": args.duration_seconds,
        "platform": {
            "os": _redact(args.platform_os),
            "version": _redact(args.platform_version),
            "architecture": _redact(args.platform_arch),
            "libc": _redact(args.platform_libc),
        },
        "command": _redact(args.command, home=args.redact_home, host=args.redact_host),
        "implementation": _redacted_list(args.implementation, args),
        "automated_tests": _redacted_list(args.automated_test, args),
        "decisions": _redacted_list(args.decision, args),
        "errors": _redacted_list(args.error, args),
        "documentation_lookups": _redacted_list(args.documentation_lookup, args),
        "result": _redact(args.result, home=args.redact_home, host=args.redact_host),
        "limitations": _redacted_list(args.limitation, args),
        "redaction": {
            "raw_auth_output_persisted": False,
            "raw_terminal_output_persisted": False,
            "secrets_allowed": False,
            "host_identifier_redacted": bool(args.redact_host),
            "home_path_redacted": bool(args.redact_home),
        },
    }
    candidate_commit = _candidate_commit(args.candidate_commit)
    if candidate_commit is not None:
        result["candidate_commit"] = candidate_commit
    if args.uat_id:
        result["uat_id"] = args.uat_id
    return result


def main() -> int:
    args = _parser().parse_args()
    id_pattern = re.compile(r"^[A-Z][A-Z0-9-]+$")
    criterion_pattern = re.compile(
        r"^(?:AC|TEST|UAT|GATE|CONSTRAINT|MILESTONE|DELIVERABLES|COMPLETION)-[A-Z0-9-]+$"
    )
    record_id = args.record_id or args.uat_id
    criterion_ids = args.criterion_id or ([args.uat_id] if args.uat_id else [])
    if not record_id or not id_pattern.fullmatch(record_id):
        raise SystemExit("error: record ID must use uppercase letters, digits, and hyphens")
    if not criterion_ids:
        raise SystemExit("error: --record-id requires at least one --criterion-id")
    if any(not criterion_pattern.fullmatch(value) for value in criterion_ids):
        raise SystemExit("error: invalid --criterion-id")
    result = build_result(args)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
