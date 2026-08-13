"""Honesty and completeness contracts for the GA evidence generator."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "ga_evidence.py"
GOAL = ROOT / "docs" / "GOAL_GENERAL_AVAILABILITY.md"
MAPPING = ROOT / "docs" / "ga-evidence-map.json"
HEADING = re.compile(
    r"^###\s+((?:AC|TEST|UAT)-[A-Z0-9-]+)(?:\s+—.*)?\s*$", re.MULTILINE
)
CURRENT_VERSION = re.search(
    r'^version\s*=\s*"([^"]+)"\s*$',
    (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    re.MULTILINE,
).group(1)


def _result(*criterion_ids: str, status: str = "passed") -> dict[str, object]:
    return {
        "schema_version": 2,
        "record_id": "UAT-006",
        "uat_id": "UAT-006",
        "candidate_version": CURRENT_VERSION,
        "criterion_ids": list(criterion_ids),
        "status": status,
        "started_at": "2026-08-09T00:00:00Z",
        "ended_at": "2026-08-09T00:00:01Z",
        "duration_seconds": 1,
        "platform": {
            "os": "ubuntu",
            "version": "22.04",
            "architecture": "x86_64",
            "libc": "glibc 2.35",
        },
        "command": "reproducible-command",
        "implementation": [],
        "automated_tests": [],
        "decisions": [],
        "errors": [],
        "documentation_lookups": [],
        "result": "observed result",
        "limitations": [],
        "redaction": {
            "raw_auth_output_persisted": False,
            "raw_terminal_output_persisted": False,
            "secrets_allowed": False,
            "host_identifier_redacted": True,
            "home_path_redacted": True,
        },
    }


def test_report_contains_every_goal_heading_and_never_passes_from_mapping() -> None:
    result = subprocess.run(
        ["python3", str(GENERATOR)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    criterion_ids = HEADING.findall(GOAL.read_text(encoding="utf-8"))
    assert criterion_ids
    for criterion_id in criterion_ids:
        assert f"| {criterion_id} | Not run |" in result.stdout
    for supplemental in (
        "GATE-RELEASE",
        "CONSTRAINT-IMPLEMENTATION",
        "MILESTONE-0",
        "MILESTONE-4",
        "DELIVERABLES-GA",
        "COMPLETION-W",
    ):
        assert f"| {supplemental} | Not run |" in result.stdout
    assert "Strict completion: no" in result.stdout
    assert f"Candidate version: `{CURRENT_VERSION}`" in result.stdout
    for number in range(1, 7):
        criterion = f"UAT-{number:03d}"
        assert f"scripts/uat/uat-{number:03d}-" in result.stdout.split(
            f"| {criterion} | Not run |", 1
        )[1].split("\n", 1)[0]


def test_redacted_result_can_mark_only_its_criterion_passed(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "uat-006.json").write_text(
        json.dumps(_result("UAT-006")), encoding="utf-8"
    )

    result = subprocess.run(
        ["python3", str(GENERATOR), "--evidence-dir", str(evidence_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "| UAT-006 | Passed |" in result.stdout
    assert "| UAT-001 | Not run |" in result.stdout
    assert "observed result" in result.stdout
    _assert_other_candidate_is_ignored(tmp_path / "mismatched-version")
    _assert_matching_older_pass_can_win(tmp_path / "matching-version")
    _assert_commit_binding(tmp_path / "commit-binding")
    _assert_missing_candidate_is_rejected(tmp_path / "missing-binding")


def test_stale_failed_or_blocked_result_cannot_classify_new_candidate(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    for status in ("failed", "blocked"):
        record = _result("UAT-006", status=status)
        record["candidate_version"] = "0.11.0"
        record["record_id"] = f"STALE-{status.upper()}"
        record["ended_at"] = f"2026-08-09T00:00:0{1 if status == 'failed' else 2}Z"
        (evidence_dir / f"{status}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

    result = subprocess.run(
        [
            "python3",
            str(GENERATOR),
            "--evidence-dir",
            str(evidence_dir),
            "--candidate-version",
            CURRENT_VERSION,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "| UAT-006 | Not run |" in result.stdout
    assert "| UAT-006 | Failed |" not in result.stdout
    assert "| UAT-006 | Blocked |" not in result.stdout
    assert "declares candidate 0.11.0" in result.stdout


def test_strict_mode_fails_with_any_gap_and_passes_complete_small_goal(
    tmp_path: Path,
) -> None:
    incomplete = subprocess.run(
        ["python3", str(GENERATOR), "--strict"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert incomplete.returncode == 1
    assert "evidence incomplete" in incomplete.stderr

    goal = tmp_path / "goal.md"
    goal.write_text("### AC-DEMO-001 — One\n\n### TEST-DEMO\n", encoding="utf-8")
    mapping = tmp_path / "map.json"
    mapping.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "prefix": "AC-DEMO-",
                        "implementation": ["demo.py"],
                        "automated_tests": ["tests/test_demo.py"],
                        "commands": ["pytest tests/test_demo.py"],
                    }
                ],
                "criteria": {
                    "TEST-DEMO": {
                        "implementation": ["demo.py"],
                        "automated_tests": ["tests/test_demo.py"],
                        "commands": ["pytest tests/test_demo.py"],
                    }
                },
                "supplemental_criteria": {},
            }
        ),
        encoding="utf-8",
    )
    evidence_dir = tmp_path / "complete"
    evidence_dir.mkdir()
    (evidence_dir / "result.json").write_text(
        json.dumps(_result("AC-DEMO-001", "TEST-DEMO")), encoding="utf-8"
    )

    complete = subprocess.run(
        [
            "python3",
            str(GENERATOR),
            "--goal",
            str(goal),
            "--map",
            str(mapping),
            "--evidence-dir",
            str(evidence_dir),
            "--strict",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert complete.returncode == 0, complete.stderr
    assert "Strict completion: yes" in complete.stdout


def _assert_other_candidate_is_ignored(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)
    stale = _result("UAT-006")
    stale["candidate_version"] = "0.11.0"
    (evidence_dir / "stale.json").write_text(json.dumps(stale), encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(GENERATOR),
            "--evidence-dir",
            str(evidence_dir),
            "--candidate-version",
            CURRENT_VERSION,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "| UAT-006 | Not run |" in result.stdout
    assert "Ignored mismatched Passed criterion claims: 1" in result.stdout
    assert "declares candidate 0.11.0" in result.stdout


def _assert_matching_older_pass_can_win(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)
    matching = _result("UAT-006")
    matching["ended_at"] = "2026-08-09T00:00:01Z"
    matching["result"] = "matching candidate result"
    stale = _result("UAT-006")
    stale["candidate_version"] = "9.9.9"
    stale["ended_at"] = "2026-08-10T00:00:01Z"
    stale["result"] = "newer stale result"
    (evidence_dir / "matching.json").write_text(json.dumps(matching), encoding="utf-8")
    (evidence_dir / "stale.json").write_text(json.dumps(stale), encoding="utf-8")

    result = subprocess.run(
        ["python3", str(GENERATOR), "--evidence-dir", str(evidence_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "| UAT-006 | Passed |" in result.stdout
    assert "matching candidate result" in result.stdout
    assert "newer stale result" not in result.stdout


def _assert_commit_binding(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)
    expected = "a" * 40
    record = _result("UAT-006")
    (evidence_dir / "result.json").write_text(json.dumps(record), encoding="utf-8")

    missing = subprocess.run(
        [
            "python3",
            str(GENERATOR),
            "--evidence-dir",
            str(evidence_dir),
            "--candidate-commit",
            expected,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 0, missing.stderr
    assert "| UAT-006 | Not run |" in missing.stdout
    assert "declares no commit" in missing.stdout

    record["candidate_commit"] = "b" * 40
    (evidence_dir / "result.json").write_text(json.dumps(record), encoding="utf-8")
    wrong = subprocess.run(
        [
            "python3",
            str(GENERATOR),
            "--evidence-dir",
            str(evidence_dir),
            "--candidate-commit",
            expected,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong.returncode == 0, wrong.stderr
    assert "| UAT-006 | Not run |" in wrong.stdout

    record["candidate_commit"] = expected
    (evidence_dir / "result.json").write_text(json.dumps(record), encoding="utf-8")
    matching = subprocess.run(
        [
            "python3",
            str(GENERATOR),
            "--evidence-dir",
            str(evidence_dir),
            "--candidate-commit",
            expected,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert matching.returncode == 0, matching.stderr
    assert "| UAT-006 | Passed |" in matching.stdout


def _assert_missing_candidate_is_rejected(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)
    record = _result("UAT-006")
    record.pop("candidate_version")
    (evidence_dir / "unbound.json").write_text(json.dumps(record), encoding="utf-8")

    result = subprocess.run(
        ["python3", str(GENERATOR), "--evidence-dir", str(evidence_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "missing candidate_version" in result.stderr


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("raw_auth_output_persisted", "raw authentication output"),
        ("raw_terminal_output_persisted", "raw terminal output"),
    ],
)
def test_generator_rejects_evidence_that_persists_raw_output(
    tmp_path: Path, field: str, message: str
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    record = _result("UAT-001")
    record["redaction"][field] = True  # type: ignore[index]
    (evidence_dir / "unsafe.json").write_text(json.dumps(record), encoding="utf-8")

    result = subprocess.run(
        ["python3", str(GENERATOR), "--evidence-dir", str(evidence_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert f"{message} must not be persisted" in result.stderr


def test_generator_rejects_secret_shaped_value_despite_safe_declaration(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    record = _result("UAT-001")
    record["result"] = "device_code=ABCD-EFGH"
    (evidence_dir / "unsafe.json").write_text(json.dumps(record), encoding="utf-8")

    result = subprocess.run(
        ["python3", str(GENERATOR), "--evidence-dir", str(evidence_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "secret-shaped value" in result.stderr
