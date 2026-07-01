"""Lifecycle hooks — shell commands J.A.R.N. runs automatically on events.

This is a core "reliable" feature: e.g. lint after every edit, run tests before
a commit. Hooks are declared in config (see :class:`jarn.config.schema.HookSpec`)
and executed by :class:`HookRunner`. A *blocking* hook that exits non-zero
aborts the action that triggered it (e.g. block the commit if tests fail).
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path

from jarn.config.schema import HookSpec

_log = logging.getLogger("jarn")

#: Env vars a hook subprocess gets by default. Deliberately minimal: no
#: ``*_API_KEY`` / ``*_TOKEN`` / cloud-cred vars, so a compromised hook can't
#: exfiltrate secrets the user happens to have exported. ``JARN_*`` is included
#: (config/home paths, never secrets). Anything else must be declared via
#: ``extra_env`` (or ``hook_inherit_env: true`` to restore the old behavior).
_ALLOWED_ENV: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TZ",
    }
)


def _base_env() -> dict[str, str]:
    """Minimal env for a hook subprocess: allowlist + every ``JARN_*`` var."""
    env: dict[str, str] = {}
    for key in _ALLOWED_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    for key, val in os.environ.items():
        if key.startswith("JARN_"):
            env[key] = val
    return env


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
    """Runs hooks for a given event, scoped to a working directory.

    ``inherit_env`` defaults to ``False``: hook subprocesses get only the
    minimal :data:`_ALLOWED_ENV` allowlist (+ ``JARN_*`` + declared
    ``extra_env``), not the full ``os.environ``, so secrets exported into the
    agent's env don't leak to hook scripts. Set ``True`` to restore the old
    inherit-everything behavior (opt-in via ``hook_inherit_env: true``).
    """

    hooks: list[HookSpec]
    cwd: Path
    timeout: int = 120
    inherit_env: bool = False

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
        env: dict[str, str] = (
            {**os.environ} if self.inherit_env else _base_env()
        )
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
