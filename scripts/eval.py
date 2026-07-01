#!/usr/bin/env python
"""Smoke-eval runner for JARN.

Discovers fixtures under ``evals/fixtures/*/eval.yaml``, drives ONE headless
agent session against a throwaway copy of each fixture's seed ``repo/``, then
scores the result by running the fixture's ``checker`` as a subprocess inside
the prepared directory.

The scoring loop is decoupled from the agent driver: :func:`run_task` takes an
``agent_fn(prompt, cwd)`` callback. The real runner passes :func:`_headless_agent`
(which calls the in-process headless entry point); the unit tests inject a fake
``agent_fn`` so the harness logic can be exercised deterministically without a
live model.

This script REQUIRES a configured model + API key and costs real tokens.
It is NOT run in CI — see ``evals/README.md``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Repo layout: scripts/eval.py -> repo root is parent of scripts/.
REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "evals" / "fixtures"
BASELINE_PATH = REPO_ROOT / "evals" / "baseline.json"

# Directory entries inside a fixture's repo/ we never copy into the work dir.
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")

# AgentFn edits the prepared repo in place and returns optional run stats
# ({"turns": int, "cost_usd": float}), or None. It must not raise on a normal run.
AgentFn = Callable[[str, Path], "dict[str, Any] | None"]


@dataclass(slots=True)
class Fixture:
    """A parsed fixture: its directory plus the fields from eval.yaml."""

    name: str
    path: Path
    prompt: str
    checker: str
    timeout_s: int
    #: Files the agent must NOT effectively change — the authoritative tests and
    #: graders. They are restored from the seed ``repo/`` before scoring so an
    #: agent can't "pass" by rewriting the tests to match buggy output.
    protected: list[str]

    @property
    def repo_dir(self) -> Path:
        return self.path / "repo"

    @property
    def solution_dir(self) -> Path:
        return self.path / "_solution"


@dataclass(slots=True)
class TaskResult:
    """The outcome of running (and scoring) one fixture."""

    fixture: str
    passed: bool
    turns: int
    cost_cents: float
    duration_s: float
    tool_calls: int = 0
    #: How many source files the agent actually changed (added/modified),
    #: excluding the restored ``protected`` files. 0 = it never touched the code.
    files_changed: int = 0


def discover_fixtures(fixtures_dir: Path = FIXTURES_DIR) -> list[Fixture]:
    """Find every ``<dir>/eval.yaml`` and parse it into a :class:`Fixture`."""
    out: list[Fixture] = []
    for yaml_path in sorted(fixtures_dir.glob("*/eval.yaml")):
        raw = yaml.safe_load(yaml_path.read_text()) or {}
        out.append(
            Fixture(
                name=str(raw["name"]),
                path=yaml_path.parent,
                prompt=str(raw["prompt"]).strip(),
                checker=str(raw["checker"]).strip(),
                timeout_s=int(raw.get("timeout_s", 60)),
                protected=[str(p) for p in (raw.get("protected") or [])],
            )
        )
    return out


def _prepare_workdir(fixture: Fixture, dest: Path) -> None:
    """Copy the fixture's seed ``repo/`` into ``dest`` (which must not exist)."""
    shutil.copytree(fixture.repo_dir, dest, ignore=_COPY_IGNORE)


class CheckerRejected(ValueError):
    """Raised when a fixture's ``checker`` is not a safe, allowlisted command."""


#: The only program names a fixture checker may invoke. Everything runs through
#: the interpreter (no arbitrary binaries), and the command is executed with
#: ``shell=False`` so shell metacharacters have no meaning.
_ALLOWED_CHECKER_PROGRAMS = frozenset({"python", "python3", "pytest"})

#: Tokens that betray an attempt at shell injection / chaining. Even though we
#: run with ``shell=False`` (which already neuters them), we reject them outright
#: so a malicious checker fails loudly at discovery rather than silently.
_SHELL_METACHARS = frozenset(";|&$`<>(){}\n\\\"'")


def validate_checker(checker: str) -> list[str]:
    """Validate a fixture checker and return its argv (for ``shell=False``).

    A checker is accepted only when it (1) contains no shell metacharacters and
    (2) invokes one of :data:`_ALLOWED_CHECKER_PROGRAMS`. This closes the RCE
    path where a fixture's ``checker:`` field — untrusted input from a YAML file
    that could be CI- or user-supplied — was run verbatim with ``shell=True``.
    Raises :class:`CheckerRejected` otherwise.
    """
    if any(ch in _SHELL_METACHARS for ch in checker):
        raise CheckerRejected(
            f"checker contains shell metacharacters (rejected): {checker!r}"
        )
    try:
        argv = shlex.split(checker)
    except ValueError as exc:
        raise CheckerRejected(f"checker is not a valid command: {checker!r} ({exc})") from exc
    if not argv:
        raise CheckerRejected("checker is empty")
    program = Path(argv[0]).name
    if program not in _ALLOWED_CHECKER_PROGRAMS:
        raise CheckerRejected(
            f"checker program {program!r} is not allowlisted "
            f"(allowed: {sorted(_ALLOWED_CHECKER_PROGRAMS)}): {checker!r}"
        )
    return argv


def run_checker(checker: str, cwd: Path, timeout_s: int) -> bool:
    """Run ``checker`` as a subprocess inside ``cwd``. Return True iff exit 0.

    The checker is validated and split into an argv (see :func:`validate_checker`)
    and run with ``shell=False`` — no shell is involved, so metacharacters in a
    hostile fixture cannot inject commands. A rejected checker scores FAIL.
    """
    try:
        argv = validate_checker(checker)
    except CheckerRejected as exc:
        print(f"checker rejected: {exc}", file=sys.stderr)
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            shell=False,
            cwd=str(cwd),
            timeout=timeout_s,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _restore_protected(fixture: Fixture, work: Path) -> None:
    """Overwrite the fixture's protected files in ``work`` from the seed.

    This is the anti-gaming guard: the agent may edit source freely, but the
    authoritative tests/graders are reset to their canonical seed copies before
    we score — so rewriting a test to match buggy output, or weakening the
    grader, has no effect on the verdict.
    """
    for rel in fixture.protected:
        src = fixture.repo_dir / rel
        if src.is_file():
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def run_task(fixture: Fixture, agent_fn: AgentFn) -> TaskResult:
    """Drive one fixture end to end: prepare -> agent edits -> restore -> score.

    ``agent_fn`` performs the edit against the prepared work dir and returns
    optional run stats (``{"turns", "cost_usd"}``) — a return value, not a side
    channel, so tests exercise the same path. Protected files are restored from
    the seed *after* the agent runs and *before* scoring (anti-gaming).
    """
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"jarn-eval-{fixture.name}-") as tmp:
        work = Path(tmp) / "repo"
        _prepare_workdir(fixture, work)

        before = _snapshot_tree(work)
        stats = agent_fn(fixture.prompt, work) or {}
        # Count changes the agent made, ignoring the protected files (which are
        # restored next) — this tells "did it touch the actual code at all?".
        files_changed = _count_changes(before, _snapshot_tree(work), fixture.protected)

        _restore_protected(fixture, work)
        passed = run_checker(fixture.checker, work, fixture.timeout_s)

    return TaskResult(
        fixture=fixture.name,
        passed=passed,
        turns=int(stats.get("turns", 0)),
        cost_cents=float(stats.get("cost_usd", 0.0)) * 100.0,
        duration_s=round(time.monotonic() - start, 2),
        tool_calls=int(stats.get("tool_calls", 0)),
        files_changed=files_changed,
    )


def _snapshot_tree(root: Path) -> dict[str, int]:
    """Map each file's relative path -> a content hash (skips __pycache__)."""
    import hashlib

    snap: dict[str, int] = {}
    for p in root.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            rel = str(p.relative_to(root))
            snap[rel] = hash(hashlib.sha1(p.read_bytes()).hexdigest())
    return snap


def _count_changes(
    before: dict[str, int], after: dict[str, int], protected: list[str]
) -> int:
    """How many non-protected files were added or had their contents change."""
    protected_set = set(protected)
    changed = 0
    for rel, h in after.items():
        if rel in protected_set:
            continue
        if before.get(rel) != h:
            changed += 1
    return changed


# --------------------------------------------------------------------------- #
# Real agent driver (headless). Skipped by the unit tests.                     #
# --------------------------------------------------------------------------- #


def _load_eval_config(model_override: str | None = None) -> Any:
    """Load config set for unattended edits on the host.

    Sets ``permission_mode=yolo`` directly (no prompts) rather than the ``ci``
    profile — ``ci`` now requires the docker backend (and fails closed if Docker
    is unavailable), which is heavier than eval fixtures need. Eval fixtures are
    our own throwaway code in a temp dir, so honest host execution is fine; we
    just don't claim isolation we don't have.

    The config is loaded eval-neutral, NOT scoped to the JARN dev repo: we pass
    ``project_root=None`` so the JARN.md project context and the dev repo's
    ``.jarn/config.yaml`` (hooks, permission rules, etc.) never bleed into the
    fixture run. The eval must measure the agent on the fixture alone — a hook
    configured in this repo firing on a tempdir, or the dev-repo system context
    being shown to the model, would silently corrupt the result.

    Returns the Config, or raises RuntimeError with a clear message if no global
    config exists (i.e. ``jarn setup`` was never run).
    """
    from jarn.config import paths
    from jarn.config.loader import load_config
    from jarn.config.schema import PermissionMode

    if not paths.global_config_path().is_file():
        raise RuntimeError("no configuration found — run `jarn setup` first.")

    # project_root=None → global config only; no dev-repo project context/keys.
    cfg = load_config(project_root=None, project_trusted=False)
    cfg.permission_mode = PermissionMode.YOLO
    if model_override:
        # routing.main takes precedence over default_model in resolution, so set
        # both to be unambiguous — lets an experiment pin a model without touching
        # the user's ~/.jarn/config.yaml.
        cfg.default_model = model_override
        cfg.routing.main = model_override
    return cfg


def _make_headless_agent(
    config: Any, system_prompt_override: str | None = None
) -> AgentFn:
    """Build the real ``agent_fn`` that drives an in-process headless turn.

    Returns run stats (turns, cost) as the callback's return value, which
    :func:`run_task` records — no side channel. ``system_prompt_override`` is
    forwarded to the runtime for the harness-prompt A/B (see
    :func:`run_harness_comparison`).
    """
    import asyncio

    from jarn.headless import _run_headless

    def agent_fn(prompt: str, cwd: Path) -> dict[str, Any] | None:
        result = asyncio.run(
            _run_headless(
                prompt, config, cwd, project_trusted=True,
                system_prompt_override=system_prompt_override,
            )
        )
        return {
            "turns": result.turns,
            "cost_usd": result.cost,
            "tool_calls": result.tool_calls,
        }

    return agent_fn


# --------------------------------------------------------------------------- #
# Harness-prompt A/B: same model + same tools + same loop, prompt is the ONLY  #
# variable. Isolates the contribution of J.A.R.N.'s system prompt.             #
# --------------------------------------------------------------------------- #

#: (arm label, system_prompt_override). ``None`` = J.A.R.N.'s full assembled
#: prompt; ``""`` = empty (DeepAgents' own default agent instructions still
#: apply — there is no "zero prompt" floor with a tool-using agent).
HARNESS_ARMS: list[tuple[str, str | None]] = [
    ("jarn-full", None),
    (
        "minimal",
        "You are a coding assistant working in a terminal. "
        "Use the available tools to complete the task.",
    ),
    ("empty", ""),
]


def run_harness_comparison(
    fixtures: list[Fixture], config: Any, repeat: int
) -> list[dict[str, Any]]:
    """Run every (arm × fixture) ``repeat`` times; return aggregated rows.

    The only thing that differs between arms is the system prompt — model,
    tools, backend, loop, fixture seed and checker are all held constant.
    """
    rows: list[dict[str, Any]] = []
    for arm, override in HARNESS_ARMS:
        agent_fn = _make_headless_agent(config, system_prompt_override=override)
        for fixture in fixtures:
            runs: list[TaskResult] = []
            errors = 0
            for _ in range(repeat):
                # A transient model failure (stream timeout / rate-limit / 5xx) on
                # one run must NOT abort the whole matrix — record it as an errored
                # (non-passing) attempt and carry on.
                try:
                    runs.append(run_task(fixture, agent_fn))
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    print(f"    {arm}/{fixture.name} run errored: {exc}",
                          file=sys.stderr)
            n_pass = sum(1 for r in runs if r.passed)
            ok = len(runs) or 1  # avoid /0 when every attempt errored
            rows.append({
                "arm": arm,
                "fixture": fixture.name,
                "passed": n_pass,
                "n": repeat,
                "errors": errors,
                # Averages are over completed runs only (errored runs have no
                # meaningful tool/edit/time numbers).
                "avg_tool_calls": round(sum(r.tool_calls for r in runs) / ok, 1),
                "avg_files_changed": round(sum(r.files_changed for r in runs) / ok, 1),
                "avg_turns": round(sum(r.turns for r in runs) / ok, 1),
                "avg_cost_cents": round(sum(r.cost_cents for r in runs) / ok, 3),
                "avg_time_s": round(sum(r.duration_s for r in runs) / ok, 2),
            })
            errnote = f", err={errors}" if errors else ""
            print(
                f"  {arm:<10} {fixture.name:<20} {n_pass}/{repeat} pass "
                f"(tools≈{rows[-1]['avg_tool_calls']}, "
                f"edits≈{rows[-1]['avg_files_changed']}{errnote})",
                file=sys.stderr,
            )
    return rows


def _print_comparison(rows: list[dict[str, Any]]) -> None:
    header = (
        f"{'ARM':<10} {'FIXTURE':<20} {'PASS':>7} {'ERR':>4} {'TOOLS':>6} "
        f"{'EDITS':>6} {'TURNS':>6} {'COST¢':>8} {'TIME(s)':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        passed = f"{r['passed']}/{r['n']}"
        print(
            f"{r['arm']:<10} {r['fixture']:<20} {passed:>7} {r.get('errors', 0):>4} "
            f"{r['avg_tool_calls']:>6} {r['avg_files_changed']:>6} "
            f"{r['avg_turns']:>6} {r['avg_cost_cents']:>8.3f} {r['avg_time_s']:>8.2f}"
        )
    print("-" * len(header))
    # Per-arm overall pass-rate.
    print("Overall pass-rate by arm:")
    for arm, _ in HARNESS_ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm]
        p = sum(r["passed"] for r in arm_rows)
        n = sum(r["n"] for r in arm_rows)
        pct = (100.0 * p / n) if n else 0.0
        print(f"  {arm:<10} {p}/{n}  ({pct:.0f}%)")


# --------------------------------------------------------------------------- #
# Baseline + reporting.                                                        #
# --------------------------------------------------------------------------- #


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, dict[str, bool]] | None:
    """Return the baseline map, or None if the file is absent/empty."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text() or "{}")
    return data or None


def detect_regressions(
    baseline: dict[str, dict[str, bool]], results: list[TaskResult]
) -> list[str]:
    """Names of fixtures that passed in the baseline but fail now."""
    current = {r.fixture: r.passed for r in results}
    regressed: list[str] = []
    for name, entry in baseline.items():
        if entry.get("passed") and not current.get(name, False):
            regressed.append(name)
    return sorted(regressed)


def write_baseline(results: list[TaskResult], path: Path = BASELINE_PATH) -> None:
    """Persist {fixture: {passed}} so future runs can flag regressions."""
    data = {r.fixture: {"passed": r.passed} for r in results}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _print_table(results: list[TaskResult]) -> None:
    header = f"{'FIXTURE':<24} {'RESULT':<6} {'TURNS':>5} {'COST¢':>8} {'TIME(s)':>8}"
    print(header)
    print("-" * len(header))
    for r in results:
        verdict = "PASS" if r.passed else "FAIL"
        print(
            f"{r.fixture:<24} {verdict:<6} {r.turns:>5} "
            f"{r.cost_cents:>8.3f} {r.duration_s:>8.2f}"
        )
    n_pass = sum(1 for r in results if r.passed)
    print("-" * len(header))
    print(f"{n_pass}/{len(results)} passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JARN smoke-eval runner.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--fixture", help="run only the named fixture")
    parser.add_argument(
        "--model",
        help="override the model ref for this run only (does not touch "
        "~/.jarn/config.yaml), e.g. openrouter/deepseek/deepseek-v4-pro",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write evals/baseline.json from this run instead of comparing",
    )
    parser.add_argument(
        "--compare-harness",
        action="store_true",
        help="A/B the JARN system prompt vs minimal/empty (same model+tools), "
        "running each arm x fixture --repeat times",
    )
    parser.add_argument(
        "--repeat", type=int, default=3,
        help="repeats per (arm, fixture) in --compare-harness mode (default 3)",
    )
    args = parser.parse_args(argv)

    fixtures = discover_fixtures()
    if args.fixture:
        fixtures = [f for f in fixtures if f.name == args.fixture]
        if not fixtures:
            print(f"error: no fixture named {args.fixture!r}", file=sys.stderr)
            return 2

    try:
        config = _load_eval_config(model_override=args.model)
    except RuntimeError as exc:
        print(f"skip: {exc}", file=sys.stderr)
        print("eval requires a configured model + API key; skipping.", file=sys.stderr)
        return 2

    if args.compare_harness:
        # Small/self-hosted endpoints can stall mid-stream on a long task; give
        # the stream a longer leash for the eval so a transient pause isn't a
        # hard failure (per-run errors are tolerated below regardless). Respect an
        # explicit user setting.
        import os
        os.environ.setdefault("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "300")
        print(
            f"comparing {len(HARNESS_ARMS)} arms x {len(fixtures)} fixtures "
            f"x {args.repeat} repeats (model + tools held constant)…",
            file=sys.stderr,
        )
        rows = run_harness_comparison(fixtures, config, args.repeat)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            _print_comparison(rows)
        return 0

    agent_fn = _make_headless_agent(config)

    results: list[TaskResult] = []
    for fixture in fixtures:
        results.append(run_task(fixture, agent_fn))

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "fixture": r.fixture,
                        "passed": r.passed,
                        "turns": r.turns,
                        "cost_cents": r.cost_cents,
                        "duration_s": r.duration_s,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        _print_table(results)

    if args.update_baseline:
        write_baseline(results)
        # To stderr when --json so it doesn't contaminate the JSON on stdout.
        print(f"baseline written to {BASELINE_PATH}", file=sys.stderr if args.json else sys.stdout)
        return 0

    baseline = load_baseline()
    if baseline is None:
        if not args.json:
            print("note: no baseline.json — run with --update-baseline to create one.")
        return 0

    regressed = detect_regressions(baseline, results)
    if regressed:
        print(f"REGRESSION: {', '.join(regressed)} passed in baseline but fail now.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
