"""Self-verification helpers.

Detects how a project builds/tests/lints so the agent (via the system prompt and
hooks) can verify its own changes. Detection is best-effort and based on common
project markers; results are advisory hints unless ``verify.gate`` is ``auto``.
"""

from __future__ import annotations

import asyncio
import json
import re
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarn.agent.session import SessionDriver

_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:", re.MULTILINE)

_NODE_SCRIPT_BUCKETS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("test",), "test"),
    (("build",), "build"),
    (("lint",), "lint"),
    (("check", "typecheck"), "lint"),
)


@dataclass(slots=True)
class ProjectCapabilities:
    test: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.test or self.build or self.lint)

    def as_prompt_block(self) -> str:
        if not self.any:
            return ""
        lines = ["# Verification commands (detected)"]
        for label, cmds in (("test", self.test), ("build", self.build), ("lint", self.lint)):
            for cmd in cmds:
                lines.append(f"- {label}: `{cmd}`")
        lines.append("\nRun the relevant command(s) to verify changes before reporting done.")
        return "\n".join(lines)


def detect_capabilities(project_root: Path) -> ProjectCapabilities:
    caps = ProjectCapabilities()
    if not project_root or not project_root.is_dir():
        return caps

    _detect_node(project_root, caps)
    _detect_python(project_root, caps)
    _detect_make(project_root, caps)
    _detect_rust_go(project_root, caps)
    return caps


def _detect_node(root: Path, caps: ProjectCapabilities) -> None:
    pkg = root / "package.json"
    if not pkg.is_file():
        return
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    scripts: dict[str, str] = data.get("scripts", {}) or {}
    runner = "npm run"
    if (root / "pnpm-lock.yaml").is_file():
        runner = "pnpm"
    elif (root / "yarn.lock").is_file():
        runner = "yarn"
    elif (root / "bun.lockb").is_file():
        runner = "bun run"
    for script_name in scripts:
        lower = script_name.lower()
        for keywords, bucket_name in _NODE_SCRIPT_BUCKETS:
            if any(kw in lower for kw in keywords):
                bucket = getattr(caps, bucket_name)
                cmd = f"{runner} {script_name}"
                if cmd not in bucket:
                    bucket.append(cmd)
                break


def _pyproject_text(root: Path) -> str:
    path = root / "pyproject.toml"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _has_pytest_config(root: Path, pyproject: str) -> bool:
    if (root / "pytest.ini").is_file():
        return True
    if "[tool.pytest.ini_options]" in pyproject:
        return True
    if "[tool.pytest]" in pyproject:
        return True
    # dev dependency on pytest
    return bool(re.search(r'["\']pytest["\']', pyproject))


def _has_ruff_config(root: Path, pyproject: str) -> bool:
    if (root / "ruff.toml").is_file():
        return True
    return "[tool.ruff]" in pyproject


def _detect_python(root: Path, caps: ProjectCapabilities) -> None:
    pyproject = _pyproject_text(root)
    has_pyproject = (root / "pyproject.toml").is_file()
    if _has_pytest_config(root, pyproject) and (
        (root / "tests").is_dir() or has_pyproject
    ):
        caps.test.append("pytest -q")
    if _has_ruff_config(root, pyproject):
        caps.lint.append("ruff check .")


def _detect_make(root: Path, caps: ProjectCapabilities) -> None:
    mk = root / "Makefile"
    if not mk.is_file():
        return
    try:
        text = mk.read_text(encoding="utf-8")
    except OSError:
        return
    targets = set(_MAKE_TARGET_RE.findall(text))
    for target, bucket in (("test", caps.test), ("build", caps.build), ("lint", caps.lint)):
        if target in targets:
            bucket.append(f"make {target}")


def _detect_rust_go(root: Path, caps: ProjectCapabilities) -> None:
    if (root / "Cargo.toml").is_file():
        caps.test.append("cargo test")
        caps.build.append("cargo build")
    if (root / "go.mod").is_file():
        caps.test.append("go test ./...")
        caps.build.append("go build ./...")


def primary_test_command(project_root: Path | None) -> str | None:
    """Return the first detected test command for *project_root*, if any."""
    if not project_root:
        return None
    caps = detect_capabilities(project_root)
    return caps.test[0] if caps.test else None


def gate_commands(project_root: Path | None) -> list[str]:
    """Ordered command set the verify gate runs: all tests, then build, then lint.

    Why a fuller set than :func:`primary_test_command`: a change can pass the tests
    yet break the build or fail lint/typecheck. If the gate runs only ``test[0]``
    those regressions pass the reliability gate silently. Running every detected
    acceptance command closes that hole. Order is test -> build -> lint (typecheck
    is bucketed into lint at detection time), preserving detection order and
    deduping so the common test-only project behaves exactly as before.
    """
    if not project_root:
        return []
    caps = detect_capabilities(project_root)
    cmds: list[str] = []
    for cmd in (*caps.test, *caps.build, *caps.lint):
        if cmd not in cmds:
            cmds.append(cmd)
    return cmds


def summarize_output(cmd: str, output: str, *, exit_code: int = 0) -> str:
    """Extract a one-line summary from command output.

    Returns a short human-readable status string. Never raises — falls back to
    ``"exit {exit_code}"`` on any parse error.
    """
    try:
        cmd_lower = cmd.lower()
        if "pytest" in cmd_lower:
            m = re.search(r"\d+ (?:passed|failed).*", output)
            if m:
                return m.group(0)
        elif "cargo" in cmd_lower:
            m = re.search(r"test result: \S+\. (.+)", output, re.MULTILINE)
            if m:
                return m.group(1)
        elif "go test" in cmd_lower:
            m = re.search(r"^(ok|FAIL)\s+\S+", output, re.MULTILINE)
            if m:
                return m.group(0)
        elif any(x in cmd_lower for x in ("npm", "pnpm", "yarn", "bun")):
            m = re.search(r"Tests:\s+(.+)", output, re.MULTILINE)
            if m:
                return m.group(1)
        return f"exit {exit_code}"
    except Exception:  # noqa: BLE001
        return f"exit {exit_code}"


@dataclass(slots=True, frozen=True)
class _CommandOutcome:
    """One command's verify result, folded together by :func:`_aggregate_outcomes`."""

    cmd: str
    ok: bool
    summary: str
    output: str
    secs: float


def _aggregate_outcomes(outcomes: list[_CommandOutcome]) -> dict[str, Any]:
    """Fold per-command results into one pass/fail ``verify`` payload.

    Every command must pass for ``ok``. On failure the payload names and shows only
    the *failing* commands so the existing one-shot repair loop receives a focused,
    combined signal. Field shape matches the single-command payload (``cmd``,
    ``ok``, ``mode``, ``summary``, ``secs``, optional ``full_output``) so downstream
    consumers (session repair loop, renderer, headless) need no changes.
    """
    total_secs = round(sum(o.secs for o in outcomes), 1)
    failed = [o for o in outcomes if not o.ok]
    if not failed:
        summary = (
            outcomes[0].summary
            if len(outcomes) == 1
            else f"{len(outcomes)} checks passed"
        )
        return {
            "cmd": " && ".join(o.cmd for o in outcomes),
            "ok": True,
            "mode": "auto",
            "summary": summary,
            "secs": float(total_secs),
        }

    summary = (
        failed[0].summary
        if len(failed) == 1
        else "; ".join(f"{o.cmd}: {o.summary}" for o in failed)
    )
    data: dict[str, Any] = {
        "cmd": " && ".join(o.cmd for o in failed),
        "ok": False,
        "mode": "auto",
        "summary": summary,
        "secs": float(total_secs),
    }
    full_output = "\n\n".join(f"$ {o.cmd}\n{o.output}" for o in failed if o.output)
    if full_output:
        # Cap output to avoid attaching megabytes to NOTICE; tail-20k for pager display.
        data["full_output"] = full_output[-20_000:]
    return data


async def _execute_and_aggregate(
    executor: Callable[[str], Any], cmds: list[str]
) -> dict[str, Any]:
    """Run each command in order and aggregate into a single verify payload."""
    outcomes: list[_CommandOutcome] = []
    for cmd in cmds:
        t0 = _time.monotonic()
        resp = await asyncio.to_thread(executor, cmd)
        secs = round(_time.monotonic() - t0, 1)
        exit_code = int(getattr(resp, "exit_code", 1))
        output = (getattr(resp, "output", "") or "").strip()
        outcomes.append(
            _CommandOutcome(
                cmd=cmd,
                ok=exit_code == 0,
                summary=summarize_output(cmd, output, exit_code=exit_code),
                output=output,
                secs=float(secs),
            )
        )
    return _aggregate_outcomes(outcomes)


async def verify_after_edit(driver: SessionDriver, tool_name: str) -> Any | None:
    """Apply the configured verify gate after a write/edit tool completes.

    Returns a :class:`~jarn.agent.events.Event` NOTICE to yield, or ``None``.
    Structured events (``data={"verify": ...}``) are emitted for every configured
    path, including denied/refused/unavailable auto verification. That lets the
    session enforce auto verification as a completion contract rather than silently
    dropping a skipped acceptance check.
    """
    from jarn.agent.events import ApprovalRequest, Event, EventKind
    from jarn.permissions import Action, ActionKind, Decision

    gate = getattr(driver, "verify_gate", "off")
    if gate == "off" or tool_name not in ("write_file", "edit_file"):
        return None

    cmds = gate_commands(getattr(driver, "project_root", None))
    if not cmds:
        return None

    # Joined display for suggest/unavailable badges; the copy-pasteable full set.
    display = " && ".join(cmds)

    if gate == "suggest":
        return Event(
            EventKind.NOTICE,
            data={"verify": {"cmd": display, "mode": "suggest"}},
        )

    # auto — every command runs through the same permission policy as an agent
    # shell command. Pre-authorize the whole set before running any, so the gate
    # stays atomic: a single denied/refused command blocks without half-running.
    for cmd in cmds:
        action = Action(ActionKind.SHELL, target=cmd, tool="execute")
        result = driver.engine.evaluate(action)
        if result.decision is Decision.DENY:
            return Event(
                EventKind.NOTICE,
                data={"verify": {
                    "cmd": cmd,
                    "ok": False,
                    "mode": "blocked",
                    "summary": f"verification denied: {result.reason}",
                    "secs": 0.0,
                }},
            )
        if result.decision is Decision.ASK:
            reply = await driver.approver(
                ApprovalRequest(
                    action=action,
                    result=result,
                    description=f"run verification command: {cmd}",
                    args={"command": cmd},
                )
            )
            if not reply.approved:
                reason = reply.message or result.reason
                return Event(
                    EventKind.NOTICE,
                    data={"verify": {
                        "cmd": cmd,
                        "ok": False,
                        "mode": "refused",
                        "summary": f"verification refused: {reason}",
                        "secs": 0.0,
                    }},
                )
            driver.engine.remember(action, reply.scope)

    executor: Callable[[str], Any] | None = getattr(driver, "verify_executor", None)
    if executor is None:
        return Event(
            EventKind.NOTICE,
            data={"verify": {
                "cmd": display,
                "ok": False,
                "mode": "unavailable",
                "summary": "no verification execution backend",
                "secs": 0.0,
            }},
        )

    verify_data = await _execute_and_aggregate(executor, cmds)
    return Event(EventKind.NOTICE, data={"verify": verify_data})
