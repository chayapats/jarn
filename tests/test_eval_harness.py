"""Offline unit tests for the smoke-eval harness (scripts/eval.py).

No live model is involved: the determinism checks run the real fixture
checkers as subprocesses, and the scoring loop is exercised with a fake
``agent_fn``. This is what the decoupled :func:`run_task(fixture, agent_fn)`
design buys us.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_eval_module():
    """Import scripts/eval.py by path (it is not an installed package)."""
    path = REPO_ROOT / "scripts" / "eval.py"
    spec = importlib.util.spec_from_file_location("jarn_eval_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eval_mod = _load_eval_module()


# --------------------------------------------------------------------------- #
# Fixture discovery.                                                           #
# --------------------------------------------------------------------------- #

EXPECTED_FIXTURES = {
    "add-missing-test",
    "fix-failing-test",
    "fix-off-by-one",
    "implement-stub",
}


def test_discover_finds_all_fixtures() -> None:
    fixtures = eval_mod.discover_fixtures()
    names = {f.name for f in fixtures}
    assert names == EXPECTED_FIXTURES


def test_discover_parses_eval_yaml() -> None:
    by_name = {f.name: f for f in eval_mod.discover_fixtures()}
    for name, fx in by_name.items():
        assert fx.prompt, f"{name} has empty prompt"
        assert fx.checker, f"{name} has empty checker"
        assert fx.timeout_s > 0, f"{name} has non-positive timeout"
        assert fx.repo_dir.is_dir()
        assert fx.solution_dir.is_dir()


# --------------------------------------------------------------------------- #
# Determinism contract: checker FAILS on seed repo/, PASSES on _solution/.     #
# --------------------------------------------------------------------------- #


def _run_checker_in(checker: str, src: Path, tmp: Path, timeout_s: int) -> bool:
    """Copy ``src`` into a fresh dir and run the checker there.

    We never run the checker against the fixture dir in place (that would
    leave __pycache__ artifacts); we copy first, mirroring the runner.
    """
    work = tmp / "work"
    shutil.copytree(src, work, ignore=eval_mod._COPY_IGNORE)
    return eval_mod.run_checker(checker, work, timeout_s)


@pytest.mark.parametrize("fixture", eval_mod.discover_fixtures(), ids=lambda f: f.name)
def test_seed_repo_fails(fixture, tmp_path: Path) -> None:
    assert not _run_checker_in(
        fixture.checker, fixture.repo_dir, tmp_path, fixture.timeout_s
    ), f"{fixture.name}: checker unexpectedly PASSED on seed repo/"


@pytest.mark.parametrize("fixture", eval_mod.discover_fixtures(), ids=lambda f: f.name)
def test_solution_passes(fixture, tmp_path: Path) -> None:
    assert _run_checker_in(
        fixture.checker, fixture.solution_dir, tmp_path, fixture.timeout_s
    ), f"{fixture.name}: checker unexpectedly FAILED on _solution/"


# --------------------------------------------------------------------------- #
# Scoring loop with fake agent_fn.                                             #
# --------------------------------------------------------------------------- #


def _solving_agent(fixture):
    """An agent_fn that 'solves' by copying _solution/ over the work dir."""

    def agent_fn(prompt: str, cwd: Path) -> None:
        for item in fixture.solution_dir.iterdir():
            if item.name in {"__pycache__", ".pytest_cache"}:
                continue
            dest = cwd / item.name
            if item.is_dir():
                shutil.copytree(
                    item, dest, dirs_exist_ok=True, ignore=eval_mod._COPY_IGNORE
                )
            else:
                shutil.copy2(item, dest)

    return agent_fn


def _noop_agent(prompt: str, cwd: Path) -> None:
    """An agent_fn that does nothing — leaves the broken seed untouched."""
    return None


def _fixture(name: str):
    return next(f for f in eval_mod.discover_fixtures() if f.name == name)


def test_run_task_passes_with_solving_agent() -> None:
    fx = _fixture("fix-failing-test")
    result = eval_mod.run_task(fx, _solving_agent(fx))
    assert result.passed is True
    assert result.fixture == "fix-failing-test"
    assert result.duration_s >= 0.0


def test_run_task_fails_with_noop_agent() -> None:
    fx = _fixture("fix-failing-test")
    result = eval_mod.run_task(fx, _noop_agent)
    assert result.passed is False


def test_run_task_records_agent_stats() -> None:
    """Stats come from the agent_fn's RETURN value (no side channel), so the
    real write-then-read path is exercised here."""
    fx = _fixture("fix-failing-test")
    solve = _solving_agent(fx)

    def agent_with_stats(prompt: str, cwd: Path):
        solve(prompt, cwd)
        return {"turns": 3, "cost_usd": 0.05}

    result = eval_mod.run_task(fx, agent_with_stats)
    assert result.passed is True
    assert result.turns == 3
    assert result.cost_cents == pytest.approx(5.0)


def test_run_task_protected_test_cannot_be_gamed() -> None:
    """Anti-gaming: an agent that rewrites the (protected) test to match the
    buggy output — without fixing the source — still scores FAIL, because the
    protected file is restored from the seed before scoring."""
    fx = _fixture("fix-failing-test")
    assert "test_calc.py" in fx.protected

    def gaming_agent(prompt: str, cwd: Path):
        # Do NOT fix calc.py; instead rewrite the test to expect the bug's output.
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n"
            "def test_add():\n    assert add(2, 3) == -1\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        return None

    result = eval_mod.run_task(fx, gaming_agent)
    assert result.passed is False


def test_main_unknown_fixture_exits_2() -> None:
    assert eval_mod.main(["--fixture", "does-not-exist"]) == 2


def test_main_missing_config_exits_2(monkeypatch) -> None:
    from jarn.config import paths

    monkeypatch.setattr(
        paths, "global_config_path", lambda: Path("/nonexistent/jarn/config.yaml")
    )
    # Reaches _load_eval_config, which fails closed (no config) → exit 2.
    assert eval_mod.main(["--fixture", "fix-failing-test"]) == 2


# --------------------------------------------------------------------------- #
# Checker safety: no shell=True injection.                                     #
# --------------------------------------------------------------------------- #


def test_validate_checker_accepts_allowlisted() -> None:
    assert eval_mod.validate_checker("python check.py") == ["python", "check.py"]
    assert eval_mod.validate_checker("python -m pytest -q") == [
        "python", "-m", "pytest", "-q"
    ]
    assert eval_mod.validate_checker("pytest") == ["pytest"]


@pytest.mark.parametrize(
    "evil",
    [
        "python check.py; curl attacker.com",
        "python check.py && rm -rf /",
        "python check.py | sh",
        "python -c \"__import__('os').system('id')\"",
        "python check.py `id`",
        "python check.py $(id)",
        "rm -rf /",
        "bash -c 'id'",
        "echo hi > /tmp/x",
    ],
)
def test_validate_checker_rejects_injection(evil: str) -> None:
    with pytest.raises(eval_mod.CheckerRejected):
        eval_mod.validate_checker(evil)


def test_run_checker_rejects_injection_scores_fail(tmp_path: Path) -> None:
    # A malicious checker must never run; run_checker returns False (FAIL).
    sentinel = tmp_path / "pwned"
    evil = f"python -c x; touch {sentinel}"
    assert eval_mod.run_checker(evil, tmp_path, timeout_s=5) is False
    assert not sentinel.exists(), "injected command must not have executed"


def test_run_checker_runs_allowlisted_command(tmp_path: Path) -> None:
    # A real allowlisted command runs with shell=False and reports its exit code.
    (tmp_path / "check.py").write_text("raise SystemExit(0)\n")
    assert eval_mod.run_checker("python check.py", tmp_path, timeout_s=30) is True
    (tmp_path / "fail.py").write_text("raise SystemExit(1)\n")
    assert eval_mod.run_checker("python fail.py", tmp_path, timeout_s=30) is False


# --------------------------------------------------------------------------- #
# Baseline regression logic.                                                   #
# --------------------------------------------------------------------------- #


def _mk_result(name: str, passed: bool):
    return eval_mod.TaskResult(
        fixture=name, passed=passed, turns=1, cost_cents=0.0, duration_s=0.1
    )


def test_regression_detected_when_baseline_pass_now_fails() -> None:
    baseline = {"a": {"passed": True}, "b": {"passed": True}}
    results = [_mk_result("a", True), _mk_result("b", False)]
    regressed = eval_mod.detect_regressions(baseline, results)
    assert regressed == ["b"]


def test_no_regression_when_matching() -> None:
    baseline = {"a": {"passed": True}, "b": {"passed": True}}
    results = [_mk_result("a", True), _mk_result("b", True)]
    assert eval_mod.detect_regressions(baseline, results) == []


def test_no_regression_when_baseline_already_failed() -> None:
    # b failed in the baseline too, so its failure now is not a regression.
    baseline = {"a": {"passed": True}, "b": {"passed": False}}
    results = [_mk_result("a", True), _mk_result("b", False)]
    assert eval_mod.detect_regressions(baseline, results) == []


def test_load_baseline_absent_returns_none(tmp_path: Path) -> None:
    assert eval_mod.load_baseline(tmp_path / "nope.json") is None


def test_write_then_load_baseline_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    results = [_mk_result("a", True), _mk_result("b", False)]
    eval_mod.write_baseline(results, path)
    loaded = eval_mod.load_baseline(path)
    assert loaded == {"a": {"passed": True}, "b": {"passed": False}}


# --------------------------------------------------------------------------- #
# Summary JSON shape + compare/regression exit-code (T-4-9).                  #
# --------------------------------------------------------------------------- #


def test_summary_json_shape() -> None:
    """build_summary emits exactly {pass, fail, total, model, cost} with correct types."""
    results = [_mk_result("a", True), _mk_result("b", True), _mk_result("c", False)]
    summary = eval_mod.build_summary(results, model="test-model", cost_usd=0.05)

    assert set(summary.keys()) == {"pass", "fail", "total", "model", "cost"}
    assert isinstance(summary["pass"], int)
    assert isinstance(summary["fail"], int)
    assert isinstance(summary["total"], int)
    assert isinstance(summary["model"], str)
    assert isinstance(summary["cost"], float)
    assert summary["pass"] == 2
    assert summary["fail"] == 1
    assert summary["total"] == 3
    assert summary["model"] == "test-model"
    assert summary["cost"] == pytest.approx(0.05)


def test_compare_regression_exit_code(tmp_path: Path) -> None:
    """compare_summary_files returns 0 when regression ≤1, non-zero when >1."""
    baseline = {"pass": 10, "fail": 0, "total": 10, "model": "m", "cost": 0.0}
    bl = tmp_path / "baseline.json"
    bl.write_text(json.dumps(baseline))

    # No regression — same pass count.
    cur_same = tmp_path / "cur_same.json"
    cur_same.write_text(json.dumps({"pass": 10, "fail": 0, "total": 10, "model": "m", "cost": 0.0}))
    assert eval_mod.compare_summary_files(cur_same, bl) == 0

    # Exactly 1 regression — within the flaky-model tolerance.
    cur_minus1 = tmp_path / "cur_minus1.json"
    cur_minus1.write_text(json.dumps({"pass": 9, "fail": 1, "total": 10, "model": "m", "cost": 0.0}))
    assert eval_mod.compare_summary_files(cur_minus1, bl) == 0

    # >1 regression — must exit non-zero.
    cur_bad = tmp_path / "cur_bad.json"
    cur_bad.write_text(json.dumps({"pass": 8, "fail": 2, "total": 10, "model": "m", "cost": 0.0}))
    assert eval_mod.compare_summary_files(cur_bad, bl) != 0
