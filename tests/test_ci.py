"""CI/release workflow contract tests — YAML gates must stay wired."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ACTION_YML = Path(__file__).resolve().parent.parent / "action" / "action.yml"
PR_REVIEW_YML = (
    Path(__file__).resolve().parent.parent / "examples" / "github" / "pr-review.yml"
)
ISSUE_FIX_YML = (
    Path(__file__).resolve().parent.parent / "examples" / "github" / "issue-fix.yml"
)

REPO = Path(__file__).resolve().parent.parent
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
RELEASE_YML = REPO / ".github" / "workflows" / "release.yml"
NIGHTLY_YML = REPO / ".github" / "workflows" / "nightly.yml"
DEPENDABOT_YML = REPO / ".github" / "dependabot.yml"
PYPROJECT = REPO / "pyproject.toml"


def _run_lines(workflow_path: Path, job: str) -> list[str]:
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = workflow["jobs"][job]["steps"]
    return [s["run"] for s in steps if isinstance(s, dict) and "run" in s]


def _uses_names(workflow_path: Path, job: str) -> list[str]:
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = workflow["jobs"][job]["steps"]
    return [s["uses"] for s in steps if isinstance(s, dict) and "uses" in s]


def test_ci_has_mypy_step() -> None:
    run_lines = _run_lines(CI_YML, "test")
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


def test_ci_lints_scripts() -> None:
    run_lines = _run_lines(CI_YML, "test")
    assert any("ruff check src tests scripts" in line for line in run_lines), (
        "ci.yml must lint scripts/ alongside src and tests"
    )


def test_ci_has_coverage_gate() -> None:
    run_lines = _run_lines(CI_YML, "test")
    test_cmd = next(line for line in run_lines if "pytest" in line)
    assert "--cov=src/jarn" in test_cmd, "ci.yml must run pytest with --cov=src/jarn"
    assert "--cov-fail-under=" in test_cmd, "ci.yml must enforce a coverage floor"


def test_ci_has_windows_matrix() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    os_list = workflow["jobs"]["test"]["strategy"]["matrix"]["os"]
    assert "windows-latest" in os_list, "ci.yml must include windows-latest in the test matrix"


def test_ci_has_security_job() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    assert "security" in workflow["jobs"], "ci.yml must define a security job"
    run_lines = _run_lines(CI_YML, "security")
    assert any("pip-audit" in line for line in run_lines), (
        "security job must run pip-audit"
    )
    uses = _uses_names(CI_YML, "security")
    assert any("gitleaks" in name for name in uses), "security job must run gitleaks"


def test_dependabot_configured() -> None:
    config = yaml.safe_load(DEPENDABOT_YML.read_text())
    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert "pip" in ecosystems, "dependabot must watch pip/uv.lock"
    assert "npm" in ecosystems, "dependabot must watch npm/"


def test_release_has_preflight_job() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    assert "preflight" in workflow["jobs"], "release.yml must define a preflight job"


def test_release_publish_jobs_need_preflight() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    for job in ("pypi", "binaries", "npm"):
        needs = workflow["jobs"][job]["needs"]
        if isinstance(needs, str):
            needs = [needs]
        assert "preflight" in needs, f"{job} job must need preflight"


def test_release_preflight_runs_ci_gates() -> None:
    run_lines = _run_lines(RELEASE_YML, "preflight")
    joined = "\n".join(run_lines)
    assert "ruff check src tests scripts" in joined
    assert "mypy src/" in joined
    assert "pytest -q" in joined
    assert "test_packaging.py" in joined


def test_release_binaries_smoke_after_build() -> None:
    run_lines = _run_lines(RELEASE_YML, "binaries")
    joined = "\n".join(run_lines)
    assert "./dist/jarn --version" in joined
    assert "./dist/jarn doctor --json" in joined


def test_release_npm_smoke_before_publish() -> None:
    run_lines = _run_lines(RELEASE_YML, "npm")
    joined = "\n".join(run_lines)
    assert "jarn-cli-linux-x64/bin/jarn --version" in joined


def test_pyproject_pins_pyinstaller_in_build_extra() -> None:
    text = PYPROJECT.read_text()
    assert "[project.optional-dependencies]" in text
    assert re.search(r'build\s*=\s*\[\s*"pyinstaller==', text), (
        "pyproject.toml must pin pyinstaller in the build extra"
    )


def test_nightly_eval_workflow_exists() -> None:
    workflow = yaml.safe_load(NIGHTLY_YML.read_text())
    job = workflow["jobs"]["eval"]
    assert job.get("continue-on-error") is True
    steps = job["steps"]
    gate = next(s for s in steps if s.get("id") == "gate")
    assert "secrets.NIGHTLY_EVAL_ENABLED" in gate["env"]["NIGHTLY_EVAL_ENABLED"]
    run_lines = _run_lines(NIGHTLY_YML, "eval")
    assert any("scripts/eval.py" in line for line in run_lines)


# ---------------------------------------------------------------------------
# T-4-7: GitHub Action + PR bot
# ---------------------------------------------------------------------------


def test_action_yaml_valid() -> None:
    """action/action.yml parses as valid YAML, has required inputs, and pins
    jarn-cli at the correct major.minor matching version.py."""
    from jarn.version import __version__

    assert ACTION_YML.exists(), "action/action.yml must exist"
    doc = yaml.safe_load(ACTION_YML.read_text())

    # Must be a composite action with the required inputs.
    inputs = doc.get("inputs", {})
    assert "prompt" in inputs, "action must declare a 'prompt' input"
    assert inputs["prompt"].get("required") is True, "'prompt' input must be required"
    assert "api_key" in inputs, "action must declare an 'api_key' input"
    assert inputs["api_key"].get("required") is True, "'api_key' input must be required"

    # Pinned jarn-cli version must match version.py major.minor (anti-drift guard).
    major, minor = __version__.split(".")[:2]
    expected_pin = f"jarn-cli@{major}.{minor}"
    # Walk all run steps in the composite action.
    steps = doc.get("runs", {}).get("steps", [])
    install_step = next(
        (s for s in steps if isinstance(s, dict) and "run" in s and "npm i" in s["run"]),
        None,
    )
    assert install_step is not None, "action must have an npm install step"
    assert expected_pin in install_step["run"], (
        f"action must pin '{expected_pin}' — got: {install_step['run']!r}"
    )


def test_example_workflows_parse() -> None:
    """pr-review.yml and issue-fix.yml parse as valid YAML, each has a
    permissions block, and each references secrets.* (hygiene guard)."""
    for path in (PR_REVIEW_YML, ISSUE_FIX_YML):
        assert path.exists(), f"{path.name} must exist"
        doc = yaml.safe_load(path.read_text())
        raw = path.read_text()

        # Must have a permissions block somewhere in the workflow.
        assert "permissions" in doc or "permissions" in raw, (
            f"{path.name} must declare a permissions block"
        )

        # Must reference secrets.* — never a literal key.
        assert "secrets." in raw, (
            f"{path.name} must source API keys from secrets.* (hygiene guard)"
        )
