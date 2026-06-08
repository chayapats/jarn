"""The CI workflow must run mypy so type regressions fail the build."""

from __future__ import annotations

from pathlib import Path

import yaml

CI_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def test_ci_has_mypy_step() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    steps = workflow["jobs"]["test"]["steps"]
    run_lines = [s["run"] for s in steps if isinstance(s, dict) and "run" in s]
    assert any("mypy src/" in line for line in run_lines), (
        "ci.yml must invoke 'mypy src/' to gate type errors"
    )


def test_ci_mypy_runs_after_lint() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    steps = workflow["jobs"]["test"]["steps"]
    runs = [s.get("run", "") for s in steps if isinstance(s, dict)]
    lint_idx = next(i for i, r in enumerate(runs) if "ruff check" in r)
    mypy_idx = next(i for i, r in enumerate(runs) if "mypy src/" in r)
    assert mypy_idx > lint_idx, "mypy step must come after the ruff Lint step"
