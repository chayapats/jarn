"""Lifecycle hooks — shell commands J.A.R.N. runs automatically on events.

This is a core "reliable" feature: e.g. lint after every edit, run tests before
a commit. Hooks are declared in config (see :class:`jarn.config.schema.HookSpec`)
and executed by :class:`HookRunner`. A *blocking* hook that exits non-zero
aborts the action that triggered it (e.g. block the commit if tests fail).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path

from jarn.config.schema import HookSpec


class HookEvent(str, Enum):
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    POST_EDIT = "post_edit"
    PRE_COMMIT = "pre_commit"
    SESSION_START = "session_start"
    SESSION_END = "session_end"


@dataclass(slots=True, frozen=True)
class HookResult:
    spec: HookSpec
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def should_abort(self) -> bool:
        return self.spec.blocking and not self.ok


@dataclass(slots=True)
class HookRunner:
    """Runs hooks for a given event, scoped to a working directory."""

    hooks: list[HookSpec]
    cwd: Path
    timeout: int = 120

    def for_event(self, event: HookEvent, *, target: str | None = None) -> list[HookSpec]:
        out = []
        for h in self.hooks:
            if h.event != event.value:
                continue
            if h.matcher and target is not None and not fnmatch(target, h.matcher):
                continue
            out.append(h)
        return out

    def run(
        self,
        event: HookEvent,
        *,
        target: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> list[HookResult]:
        """Run all hooks for ``event``. Stops early if a blocking hook aborts."""
        results: list[HookResult] = []
        env = {**os.environ}
        env["JARN_HOOK_EVENT"] = event.value
        if target:
            env["JARN_HOOK_TARGET"] = target
        if extra_env:
            env.update(extra_env)

        for spec in self.for_event(event, target=target):
            result = self._run_one(spec, env)
            results.append(result)
            if result.should_abort:
                break
        return results

    def _run_one(self, spec: HookSpec, env: dict[str, str]) -> HookResult:
        try:
            proc = subprocess.run(
                spec.command,
                shell=True,
                cwd=str(self.cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            return HookResult(spec, proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            return HookResult(spec, 124, "", f"hook timed out after {self.timeout}s")
        except OSError as exc:
            return HookResult(spec, 127, "", str(exc))
