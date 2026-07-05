"""Backward-compatible re-exports for the agent runtime assembly seam.

Implementation lives in :mod:`jarn.agent.runtime`, :mod:`jarn.agent.backends_factory`,
and :mod:`jarn.agent.builtin_tools`.
"""

from __future__ import annotations

from jarn.agent.backends_factory import (
    SandboxUnavailable,
    _make_backend,
    _make_docker_backend,
    _make_local_backend,
    _make_sandbox_backend,
)
from jarn.agent.builtin_tools import (
    _add_wiki_tools,
    _build_repo_map_tool,
    _exit_plan_mode_tool,
    _inject_repo_map,
    _suggest_memory_tool,
    _wire_builtin_tools,
)
from jarn.agent.runtime import (
    AmbientKeyLeakError,
    JarnRuntime,
    _async_subagent_specs,
    build_runtime,
    resolved_auto_summarize_tokens,
)

__all__ = [
    "AmbientKeyLeakError",
    "JarnRuntime",
    "SandboxUnavailable",
    "build_runtime",
    "resolved_auto_summarize_tokens",
    "_add_wiki_tools",
    "_async_subagent_specs",
    "_build_repo_map_tool",
    "_exit_plan_mode_tool",
    "_inject_repo_map",
    "_make_backend",
    "_make_docker_backend",
    "_make_local_backend",
    "_make_sandbox_backend",
    "_suggest_memory_tool",
    "_wire_builtin_tools",
]
