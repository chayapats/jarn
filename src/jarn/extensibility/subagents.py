"""Custom subagents — user-defined specialists the main agent can delegate to
via the ``task`` tool. Each is a markdown file under ``.jarn/agents`` with
frontmatter; the body is the subagent's system prompt::

    ---
    name: test-writer
    description: Writes and runs unit tests for a given module.
    model: openrouter/anthropic/claude-haiku-4-5   # optional, per-task routing
    ---
    You are a meticulous test engineer. ...

These are converted to deepagents ``SubAgent`` dicts at build time. The model
ref (if any) is resolved through the same ModelFactory used everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarn.config import paths
from jarn.extensibility.frontmatter import discover, parse


@dataclass(slots=True)
class CustomSubagent:
    name: str
    description: str
    system_prompt: str
    model: str | None = None          # J.A.R.N. model ref, optional
    tools: list[str] = field(default_factory=list)
    path: Path | None = None

    def to_spec(
        self,
        model_factory: Any | None = None,
        available_tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Build a deepagents ``SubAgent`` dict.

        ``model_factory`` (a :class:`jarn.providers.ModelFactory`) resolves the
        model ref into a chat model. When absent, the ref is passed through as a
        string for deepagents to resolve.

        ``tools`` restricts which of the agent's *extra* tools (the built-in web
        tools and any MCP-loaded tools, passed in ``available_tools``) this
        subagent may call. Declaring any ``tools`` sets the deepagents ``tools``
        key, which stops the subagent from inheriting the parent's full set — so
        an empty intersection means "no network/MCP tools at all". Filesystem
        built-ins (``read_file``/``write_file``/…) are injected by deepagents
        middleware and remain available, governed by the permission engine; we
        accept their names as valid but they don't appear in this list. Unknown
        tool names raise so a typo fails fast at build time rather than silently
        granting nothing.
        """
        spec: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
        }
        if self.model:
            if model_factory is not None:
                spec["model"] = model_factory.build(self.model)
            else:
                spec["model"] = self.model
        if self.tools:
            available = {
                name: t for t in (available_tools or [])
                if (name := getattr(t, "name", ""))
            }
            builtins = _builtin_tool_names()
            unknown = [t for t in self.tools if t not in available and t not in builtins]
            if unknown:
                from jarn.config import ConfigError

                raise ConfigError(
                    f"Subagent {self.name!r} declares unknown tool(s): "
                    f"{', '.join(unknown)}. Available: "
                    f"{', '.join(sorted(set(available) | builtins))}."
                )
            spec["tools"] = [available[t] for t in self.tools if t in available]
        return spec


def _builtin_tool_names() -> frozenset[str]:
    """Names of the always-present deepagents built-ins (filesystem + planning).

    Imported lazily from :mod:`jarn.agent.permissions_bridge` (the source of
    truth) to avoid an import cycle: the ``jarn.agent`` package init pulls in the
    builder, which imports this module.
    """
    from jarn.agent.permissions_bridge import (
        INTERNAL_TOOLS,
        MUTATING_TOOLS,
        READONLY_TOOLS,
    )

    return frozenset((*MUTATING_TOOLS, *READONLY_TOOLS, *INTERNAL_TOOLS))


def agent_dirs(project_root: Path | None = None) -> list[Path]:
    dirs = [paths.global_subdir("agents")]
    pdir = paths.project_dir(project_root)
    if pdir:
        dirs.append(pdir / "agents")
    return dirs


def load_subagents(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
) -> dict[str, CustomSubagent]:
    """Load custom subagents keyed by name (project overrides global)."""
    out: dict[str, CustomSubagent] = {}
    global_dir = paths.global_subdir("agents")
    for path in discover(agent_dirs(project_root)):
        scope = "global" if str(path).startswith(str(global_dir)) else "project"
        if scope == "project" and not project_trusted:
            continue
        doc = parse(path)
        name = str(doc.meta.get("name") or path.stem)
        if not doc.body.strip():
            # A subagent with no system prompt is meaningless; skip it.
            continue
        out[name] = CustomSubagent(
            name=name,
            description=str(doc.meta.get("description", "")),
            system_prompt=doc.body,
            model=doc.meta.get("model"),
            tools=list(doc.meta.get("tools", []) or []),
            path=path,
        )
    return out
