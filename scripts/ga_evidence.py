#!/usr/bin/env python3
"""Generate an honest GA evidence matrix from the goal, map, and result files.

Mappings describe where and how to verify a criterion; they never imply that it
passed. Only a validated result artifact can set ``Passed``, and ``--strict``
fails until every extracted/supplemental criterion has passing evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOAL = ROOT / "docs" / "GOAL_GENERAL_AVAILABILITY.md"
DEFAULT_MAP = ROOT / "docs" / "ga-evidence-map.json"
CRITERION_HEADING = re.compile(
    r"^###\s+((?:AC|TEST|UAT)-[A-Z0-9-]+)(?:\s+—.*)?\s*$", re.MULTILINE
)
VALID_STATUSES = {"passed", "failed", "blocked", "not_run"}
SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|sess|key)-[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(?:token|password|api[_-]?key|device[_-]?code)"
        r"(?:[=:]\s*|\s+)(?!\[REDACTED_SECRET\])[^\s,;]+"
    ),
)
STATUS_LABELS = {
    "passed": "Passed",
    "failed": "Failed",
    "blocked": "Blocked",
    "not_run": "Not run",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", type=Path, default=DEFAULT_GOAL)
    parser.add_argument("--map", dest="mapping", type=Path, default=DEFAULT_MAP)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        action="append",
        default=[],
        help="Directory containing redacted JSON result artifacts; repeatable",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write atomically to this path; omit to print Markdown to stdout",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 unless every criterion has valid passing evidence",
    )
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _display_path(path: Path) -> str:
    """Return a reproducible path without leaking a controller home directory."""
    resolved = path.expanduser().resolve()
    for base in (ROOT, Path.cwd().resolve()):
        try:
            return str(resolved.relative_to(base))
        except ValueError:
            continue
    return path.name


def extract_criteria(goal: Path, mapping: dict[str, Any]) -> list[str]:
    try:
        goal_text = goal.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read goal {goal}: {exc}") from exc
    ids = CRITERION_HEADING.findall(goal_text)
    supplemental = mapping.get("supplemental_criteria", {})
    if not isinstance(supplemental, dict):
        raise ValueError("supplemental_criteria must be an object")
    for criterion_id in supplemental:
        if criterion_id not in ids:
            ids.append(criterion_id)
    if not ids:
        raise ValueError(f"no stable criteria found in {goal}")
    return ids


def _matches(rule: dict[str, Any], criterion_id: str) -> bool:
    exact = rule.get("criterion")
    prefix = rule.get("prefix")
    return (isinstance(exact, str) and exact == criterion_id) or (
        isinstance(prefix, str) and criterion_id.startswith(prefix)
    )


def _strings(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"mapping field {field!r} must be an array of strings")
    return value


def effective_mapping(mapping: dict[str, Any], criterion_id: str) -> dict[str, list[str]]:
    merged = {
        "implementation": [],
        "automated_tests": [],
        "commands": [],
        "platforms": [],
        "limitations": [],
    }
    rules = mapping.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise ValueError("rules must be an array of objects")
    exact = mapping.get("criteria", {})
    if not isinstance(exact, dict):
        raise ValueError("criteria must be an object")
    candidates = [rule for rule in rules if _matches(rule, criterion_id)]
    if criterion_id in exact:
        override = exact[criterion_id]
        if not isinstance(override, dict):
            raise ValueError(f"mapping for {criterion_id} must be an object")
        candidates.append(override)
    supplemental = mapping.get("supplemental_criteria", {})
    if criterion_id in supplemental:
        supplement = supplemental[criterion_id]
        if not isinstance(supplement, dict):
            raise ValueError(f"supplemental mapping for {criterion_id} must be an object")
        candidates.append(supplement)
    for candidate in candidates:
        for field in merged:
            for item in _strings(candidate.get(field), field=field):
                if item not in merged[field]:
                    merged[field].append(item)
    return merged


def _validate_record(path: Path, record: dict[str, Any]) -> None:
    record_id = record.get("record_id")
    status = record.get("status")
    criterion_ids = record.get("criterion_ids")
    if not isinstance(record_id, str) or not re.fullmatch(r"[A-Z][A-Z0-9-]+", record_id):
        raise ValueError(f"{path}: invalid or missing record_id")
    if status not in VALID_STATUSES:
        raise ValueError(f"{path}: invalid or missing status")
    if not isinstance(criterion_ids, list) or not criterion_ids or not all(
        isinstance(item, str) for item in criterion_ids
    ):
        raise ValueError(f"{path}: criterion_ids must be a non-empty string array")
    if len(set(criterion_ids)) != len(criterion_ids):
        raise ValueError(f"{path}: criterion_ids must be unique")
    redaction = record.get("redaction")
    if not isinstance(redaction, dict):
        raise ValueError(f"{path}: missing redaction declaration")
    if redaction.get("raw_auth_output_persisted") is not False:
        raise ValueError(f"{path}: raw authentication output must not be persisted")
    if redaction.get("raw_terminal_output_persisted") is not False:
        raise ValueError(f"{path}: raw terminal output must not be persisted")
    if redaction.get("secrets_allowed") is not False:
        raise ValueError(f"{path}: evidence must prohibit secrets")
    serialized = json.dumps(record, ensure_ascii=False)
    if any(pattern.search(serialized) for pattern in SECRET_PATTERNS):
        raise ValueError(f"{path}: evidence contains a secret-shaped value")


def load_evidence(directories: list[Path]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for directory in directories:
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValueError(f"evidence path is not a directory: {directory}")
        for path in sorted(directory.rglob("*.json")):
            record = _read_json(path)
            _validate_record(path, record)
            enriched = dict(record)
            enriched["_source"] = _display_path(path)
            sort_key = (str(record.get("ended_at", "")), str(path))
            enriched["_sort_key"] = sort_key
            for criterion_id in record["criterion_ids"]:
                current = selected.get(criterion_id)
                if current is None or sort_key > current["_sort_key"]:
                    selected[criterion_id] = enriched
    return selected


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def _joined(values: list[str]) -> str:
    return "<br>".join(_cell(value) for value in values) if values else "—"


def _record_strings(record: dict[str, Any], field: str) -> list[str]:
    value = record.get(field, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _platform(record: dict[str, Any] | None, fallback: list[str]) -> str:
    if record is None:
        return _joined(fallback)
    value = record.get("platform")
    if not isinstance(value, dict):
        return _joined(fallback)
    parts = [
        str(value.get("os", "unknown")),
        str(value.get("version", "unknown")),
        str(value.get("architecture", "unknown")),
        str(value.get("libc", "unknown")),
    ]
    return _cell(" / ".join(parts))


def render_report(
    *,
    goal: Path,
    mapping_path: Path,
    mapping: dict[str, Any],
    criteria: list[str],
    evidence: dict[str, dict[str, Any]],
) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    rows: list[str] = []
    for criterion_id in criteria:
        mapped = effective_mapping(mapping, criterion_id)
        record = evidence.get(criterion_id)
        status = str(record.get("status")) if record else "not_run"
        counts[status] += 1
        implementation = list(mapped["implementation"])
        tests = list(mapped["automated_tests"])
        commands = list(mapped["commands"])
        limitations = list(mapped["limitations"])
        result = "No result artifact supplied; mapping is not proof of completion."
        if record:
            implementation.extend(_record_strings(record, "implementation"))
            tests.extend(_record_strings(record, "automated_tests"))
            command = record.get("command")
            if isinstance(command, str) and command:
                commands.insert(0, command)
            limitations.extend(_record_strings(record, "limitations"))
            observed = record.get("result")
            result = str(observed) if isinstance(observed, str) else "Result omitted."
            result = f"{result} (evidence: {record['_source']})"
        if not implementation:
            limitations.append("Implementation mapping missing.")
        if not tests:
            limitations.append("Automated-test mapping missing.")
        if not commands:
            limitations.append("Reproducible command mapping missing.")
        rows.append(
            "| "
            + " | ".join(
                (
                    _cell(criterion_id),
                    STATUS_LABELS[status],
                    _joined(list(dict.fromkeys(implementation))),
                    _joined(list(dict.fromkeys(tests))),
                    _platform(record, mapped["platforms"]),
                    _joined(list(dict.fromkeys(commands))),
                    _cell(result),
                    _joined(list(dict.fromkeys(limitations))) if limitations else "None recorded",
                )
            )
            + " |"
        )

    title = str(mapping.get("title", "J.A.R.N. GA release evidence"))
    summary = ", ".join(
        f"{STATUS_LABELS[key]}: {counts[key]}"
        for key in ("passed", "failed", "blocked", "not_run")
    )
    lines = [
        f"# {title}",
        "",
        "> Generated evidence index. A mapped file or command is a verification route, not proof. "
        "Only supplied redacted result artifacts can mark a row Passed.",
        "",
        f"- Goal: `{_display_path(goal)}`",
        f"- Mapping: `{_display_path(mapping_path)}`",
        f"- Summary: {summary}",
        f"- Strict completion: {'yes' if counts['passed'] == len(criteria) else 'no'}",
        "",
        "| Criterion | Status | Implementation | Automated test | Platform | Command | Result | Limitation |",
        "|---|---|---|---|---|---|---|---|",
        *rows,
        "",
    ]
    return "\n".join(lines), counts


def _write_atomic(path: Path, content: str) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def main() -> int:
    args = _parser().parse_args()
    try:
        mapping = _read_json(args.mapping)
        if mapping.get("schema_version") != 1:
            raise ValueError("evidence map schema_version must be 1")
        criteria = extract_criteria(args.goal, mapping)
        evidence = load_evidence(args.evidence_dir)
        unknown = sorted(set(evidence) - set(criteria))
        if unknown:
            raise ValueError(f"evidence contains unknown criterion IDs: {', '.join(unknown)}")
        report, counts = render_report(
            goal=args.goal,
            mapping_path=args.mapping,
            mapping=mapping,
            criteria=criteria,
            evidence=evidence,
        )
        if args.output:
            _write_atomic(args.output, report)
            print(args.output.expanduser().resolve())
        else:
            print(report, end="")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.strict and counts["passed"] != len(criteria):
        missing = len(criteria) - counts["passed"]
        print(f"error: GA evidence incomplete: {missing} criterion/criteria are not Passed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
