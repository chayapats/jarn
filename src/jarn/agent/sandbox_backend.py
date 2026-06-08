"""Cancellable LangSmith sandbox backend — Esc can kill in-flight remote commands."""

from __future__ import annotations

import contextlib
import threading

from deepagents.backends import LangSmithSandbox
from deepagents.backends.protocol import ExecuteResponse


class CancellableLangSmithSandbox(LangSmithSandbox):
    """LangSmith sandbox whose running commands can be killed on turn cancel."""

    def __init__(self, sandbox) -> None:  # noqa: ANN001 — langsmith.Sandbox
        super().__init__(sandbox)
        self._handles: list = []
        self._handles_lock = threading.Lock()

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        effective_timeout = timeout if timeout is not None else self._default_timeout
        handle = self._sandbox.run(command, timeout=effective_timeout, wait=False)
        with self._handles_lock:
            self._handles.append(handle)
        try:
            for _ in handle:
                pass
            result = handle.result
            output = result.stdout or ""
            if result.stderr:
                output += "\n" + result.stderr if output else result.stderr
            return ExecuteResponse(
                output=output,
                exit_code=result.exit_code,
                truncated=False,
            )
        finally:
            with self._handles_lock, contextlib.suppress(ValueError):
                self._handles.remove(handle)

    def terminate_all(self) -> int:
        """Kill every still-running remote command. Returns the count."""
        with self._handles_lock:
            handles = list(self._handles)
        for handle in handles:
            with contextlib.suppress(Exception):
                handle.kill()
        return len(handles)
