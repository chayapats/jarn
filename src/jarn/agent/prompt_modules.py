"""Deterministic, observable prompt-module registry.

The thin kernel remains unconditional.  Every other prompt contributor is a
bounded module with activation, provenance, scope, trust tier, and measured
token metadata.  Authorization and verification intentionally do not live here;
they remain executable harness policy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from jarn.agent import prompts
from jarn.extensibility.skills import (
    Skill,
    auto_skill_catalog,
    render_skill_invocation,
    truncate_skill_catalog_to_budget,
)
from jarn.memory.context import (
    memory_index_context,
    project_context_block,
    resolve_context_file,
    truncate_memory_catalog_to_budget,
)
from jarn.memory.tokens import count_tokens, truncate_to_token_budget

PromptModuleScope = Literal["runtime", "session", "turn"]
PromptModuleTrust = Literal["global", "project"]
PromptModuleKind = Literal["builtin", "skill"]
Render = Callable[["PromptModuleContext"], str]
Predicate = Callable[["PromptModuleContext"], bool]
ContextText = Callable[["PromptModuleContext"], str]
Budgeter = Callable[[str, int], str]
ScopeResolver = Callable[["PromptModuleContext"], PromptModuleScope]

EXPLICIT_SKILL_BUDGET = 4096
SESSION_DATE_BUDGET = 64
_SEPARATOR = "\n\n---\n\n"


@dataclass(slots=True, frozen=True)
class PromptModuleContext:
    """Read-only inputs used to activate and render prompt modules."""

    config: Any
    project_root: Path | None
    project_trusted: bool
    skills: Mapping[str, Skill]
    explicit_scopes: Mapping[str, PromptModuleScope] = field(default_factory=dict)
    prompt_override: bool = False
    now: datetime | None = None


@dataclass(slots=True, frozen=True)
class PromptModule:
    name: str
    description: str
    priority: int
    scope: PromptModuleScope
    trust: PromptModuleTrust
    default_budget: int | None
    render: Render
    active_when: Predicate
    activation_reason: ContextText
    source: str | ContextText
    kind: PromptModuleKind = "builtin"
    user_activatable: bool = False
    runtime_eligible: bool = True
    budgeter: Budgeter = truncate_to_token_budget
    scope_for: ScopeResolver | None = None


@dataclass(slots=True, frozen=True)
class RenderedPromptModule:
    name: str
    description: str
    content: str
    token_count: int
    source: str
    scope: PromptModuleScope
    trust: PromptModuleTrust
    priority: int
    configured_budget: int | None
    activation_reason: str
    truncated: bool
    kind: PromptModuleKind = "builtin"


@dataclass(slots=True, frozen=True)
class PromptModuleStatus:
    name: str
    description: str
    active: bool
    activation_reason: str
    scope: PromptModuleScope
    trust: PromptModuleTrust
    source: str
    token_count: int
    configured_budget: int | None
    truncated: bool
    kind: PromptModuleKind
    user_activatable: bool


@dataclass(slots=True, frozen=True)
class PromptAssembly:
    text: str
    modules: tuple[RenderedPromptModule, ...]
    token_count: int


def _source(module: PromptModule, context: PromptModuleContext) -> str:
    return module.source(context) if callable(module.source) else module.source


def _scope(module: PromptModule, context: PromptModuleContext) -> PromptModuleScope:
    return module.scope_for(context) if module.scope_for is not None else module.scope


def _is_active(module: PromptModule, context: PromptModuleContext) -> bool:
    if context.prompt_override:
        return False
    if module.trust == "project" and not context.project_trusted:
        return False
    return module.active_when(context)


def render_prompt_module(
    module: PromptModule,
    context: PromptModuleContext,
    *,
    budget_override: int | None = None,
) -> RenderedPromptModule:
    """Render one active module with exact hard-cap metadata."""
    raw = module.render(context).strip()
    budget = module.default_budget if budget_override is None else budget_override
    if budget is None:
        content = raw
    else:
        content = module.budgeter(raw, budget).strip()
        # A custom budgeter must not be able to violate the common contract.
        if count_tokens(content) > budget:
            content = truncate_to_token_budget(content, budget).strip()
    return RenderedPromptModule(
        name=module.name,
        description=module.description,
        content=content,
        token_count=count_tokens(content),
        source=_source(module, context),
        scope=_scope(module, context),
        trust=module.trust,
        priority=module.priority,
        configured_budget=budget,
        activation_reason=module.activation_reason(context),
        truncated=content != raw,
        kind=module.kind,
    )


def render_date_prompt_module(content: str) -> RenderedPromptModule:
    """Wrap the driver's once-per-day date text in registry-compatible metadata."""
    bounded = truncate_to_token_budget(content.strip(), SESSION_DATE_BUDGET).strip()
    return RenderedPromptModule(
        name="session.date",
        description="Current local date, injected once per thread/day.",
        content=bounded,
        token_count=count_tokens(bounded),
        source="local clock",
        scope="session",
        trust="global",
        priority=700,
        configured_budget=SESSION_DATE_BUDGET,
        activation_reason="first turn per thread/local day",
        truncated=bounded != content.strip(),
    )


class PromptModuleRegistry:
    """Immutable deterministic module catalog and assembly engine."""

    def __init__(self, modules: Iterable[PromptModule]) -> None:
        grouped: dict[str, list[PromptModule]] = {}
        for module in modules:
            grouped.setdefault(module.name, []).append(module)
        # Duplicate names resolve independently of discovery order.  This is a
        # registry metadata choice only; skills themselves already resolve their
        # scope precedence before reaching this layer.
        selected = {
            name: min(
                candidates,
                key=lambda item: (
                    item.priority,
                    item.description,
                    item.source if isinstance(item.source, str) else "",
                ),
            )
            for name, candidates in grouped.items()
        }
        self.modules = tuple(
            sorted(selected.values(), key=lambda item: (item.priority, item.name))
        )
        self._by_name = {module.name: module for module in self.modules}
        self._budget_fields: Mapping[str, str] = {}

    def resolve(self, name: str) -> PromptModule | None:
        """Resolve exact/case-insensitive names and bare skill names."""
        needle = name.strip().lower()
        for module_name, module in self._by_name.items():
            if module_name.lower() == needle:
                return module
        if not needle.startswith("skill."):
            skill_name = f"skill.{needle}"
            for module_name, module in self._by_name.items():
                if module_name.lower() == skill_name:
                    return module
        return None

    def assemble(
        self,
        context: PromptModuleContext,
        *,
        kernel: str = prompts.BASE_SYSTEM_PROMPT,
        aggregate_budget: int | None = None,
    ) -> PromptAssembly:
        """Assemble the stable kernel plus active runtime/session modules."""
        rendered: list[RenderedPromptModule] = []
        for module in self.modules:
            if not module.runtime_eligible or not _is_active(module, context):
                continue
            item = render_prompt_module(module, context)
            if item.scope == "turn" or not item.content:
                continue
            rendered.append(item)

        def _join(items: Iterable[RenderedPromptModule]) -> str:
            parts = [kernel.strip()]
            parts.extend(item.content for item in items if item.content)
            return _SEPARATOR.join(part for part in parts if part)

        text = _join(rendered)
        if aggregate_budget is not None:
            kernel_tokens = count_tokens(kernel.strip())
            if aggregate_budget < kernel_tokens:
                raise ValueError(
                    "aggregate prompt budget is smaller than the mandatory kernel"
                )
            # Trim lowest-priority suffixes until the exact joined prompt fits.
            while count_tokens(text) > aggregate_budget:
                index = next(
                    (i for i in range(len(rendered) - 1, -1, -1) if rendered[i].content),
                    None,
                )
                if index is None:
                    break
                current = rendered[index]
                excess = count_tokens(text) - aggregate_budget
                target = max(0, current.token_count - excess - 1)
                content = truncate_to_token_budget(current.content, target).strip()
                if content == current.content:
                    content = ""
                rendered[index] = replace(
                    current,
                    content=content,
                    token_count=count_tokens(content),
                    truncated=True,
                )
                text = _join(rendered)

        return PromptAssembly(
            text=text,
            modules=tuple(rendered),
            token_count=count_tokens(text),
        )

    def statuses(
        self,
        context: PromptModuleContext,
        *,
        assembly: PromptAssembly | None = None,
    ) -> tuple[PromptModuleStatus, ...]:
        """Return active and inactive registry state with measured token costs."""
        statuses: list[PromptModuleStatus] = []
        for module in self.modules:
            active = _is_active(module, context)
            reason = (
                "disabled by wholesale prompt override"
                if context.prompt_override
                else (
                    "project is untrusted"
                    if module.trust == "project" and not context.project_trusted
                    else module.activation_reason(context)
                )
            )
            rendered = render_prompt_module(module, context) if active else None
            statuses.append(
                PromptModuleStatus(
                    name=module.name,
                    description=module.description,
                    active=active,
                    activation_reason=reason,
                    scope=_scope(module, context),
                    trust=module.trust,
                    source=_source(module, context),
                    token_count=rendered.token_count if rendered else 0,
                    configured_budget=(
                        rendered.configured_budget if rendered else module.default_budget
                    ),
                    truncated=rendered.truncated if rendered else False,
                    kind=module.kind,
                    user_activatable=module.user_activatable,
                )
            )
        if assembly is not None:
            actual = {item.name: item for item in assembly.modules}
            statuses = [
                replace(
                    status,
                    active=True,
                    activation_reason=actual[status.name].activation_reason,
                    scope=actual[status.name].scope,
                    trust=actual[status.name].trust,
                    source=actual[status.name].source,
                    token_count=actual[status.name].token_count,
                    configured_budget=actual[status.name].configured_budget,
                    truncated=actual[status.name].truncated,
                )
                if status.name in actual
                else status
                for status in statuses
            ]
        return tuple(statuses)


def _explicit_scope(context: PromptModuleContext, name: str) -> PromptModuleScope:
    return context.explicit_scopes.get(name, "turn")


def _visible_skills(context: PromptModuleContext) -> dict[str, Skill]:
    """Trust-filter skills even if a caller supplied an over-broad mapping."""
    return {
        name: skill
        for name, skill in context.skills.items()
        if context.project_trusted or skill.scope != "project"
    }


def _skill_catalog(context: PromptModuleContext) -> str:
    return auto_skill_catalog(_visible_skills(context), token_budget=None)


def _has_auto_skills(context: PromptModuleContext) -> bool:
    return any(
        skill.auto_eligible and skill.description
        for skill in _visible_skills(context).values()
    )


def _wiki_index(context: PromptModuleContext) -> str:
    if not context.config.wiki.enabled:
        return ""
    from jarn.memory.wiki import WikiStore

    full = WikiStore.build(context.project_root)
    store = (
        full
        if context.project_trusted
        else WikiStore(global_wiki_dir=full.global_wiki_dir)
    )
    text = store.index_text()
    return f"<wiki_index>\n{text.strip()}\n</wiki_index>" if text.strip() else ""


def _repo_map(context: PromptModuleContext) -> str:
    if context.config.context.repo_map != "auto" or context.project_root is None:
        return ""
    from jarn.agent.repomap import build_repo_map

    try:
        text = build_repo_map(
            context.project_root,
            token_budget=context.config.context.repo_map_tokens,
        )
    except Exception:  # noqa: BLE001 - optional context cannot block startup
        return ""
    return f"<repo_map>\n{text.strip()}\n</repo_map>" if text.strip() else ""


def _project_source(context: PromptModuleContext) -> str:
    path = resolve_context_file(
        context.project_root,
        context_files=context.config.compat.context_files,
    )
    if path is None:
        return "project guidance"
    if context.project_root is not None:
        try:
            return path.relative_to(context.project_root).as_posix()
        except ValueError:
            pass
    return str(path)


def create_prompt_module_registry(
    skills: Mapping[str, Skill] | None = None,
) -> PromptModuleRegistry:
    """Create the built-in registry plus lazy explicit skill-body modules."""
    modules: list[PromptModule] = [
        PromptModule(
            name="mode.plan",
            description="Read-only planning guidance for the active permission mode.",
            priority=100,
            scope="runtime",
            trust="global",
            default_budget=96,
            render=lambda context: prompts.mode_context(context.config.permission_mode),
            active_when=lambda context: (
                getattr(context.config.permission_mode, "value", context.config.permission_mode)
                == "plan"
            ),
            activation_reason=lambda context: (
                "permission mode is plan"
                if getattr(
                    context.config.permission_mode,
                    "value",
                    context.config.permission_mode,
                )
                == "plan"
                else "permission mode is not plan"
            ),
            source="permission mode",
        ),
        PromptModule(
            name="context.project",
            description="Trusted project guidance excerpt.",
            priority=200,
            scope="runtime",
            trust="project",
            default_budget=None,
            render=lambda context: project_context_block(
                context.project_root,
                context_files=context.config.compat.context_files,
            ),
            active_when=lambda context: resolve_context_file(
                context.project_root,
                context_files=context.config.compat.context_files,
            )
            is not None,
            activation_reason=lambda context: (
                "trusted project guidance exists"
                if context.project_trusted
                else "project is untrusted"
            ),
            source=_project_source,
        ),
        PromptModule(
            name="memory.catalog",
            description="Names and descriptions from global/project long-term memory.",
            priority=300,
            scope="runtime",
            trust="global",
            default_budget=None,
            render=lambda context: memory_index_context(
                context.project_root,
                project_trusted=context.project_trusted,
            ),
            active_when=lambda context: bool(
                memory_index_context(
                    context.project_root,
                    project_trusted=context.project_trusted,
                ).strip()
            ),
            activation_reason=lambda context: (
                "at least one trusted memory entry exists"
                if memory_index_context(
                    context.project_root,
                    project_trusted=context.project_trusted,
                ).strip()
                else "no memory entries"
            ),
            source="global/project memory indices",
            budgeter=truncate_memory_catalog_to_budget,
        ),
        PromptModule(
            name="skills.catalog",
            description="Names, descriptions, and paths for auto-eligible skills.",
            priority=400,
            scope="runtime",
            trust="global",
            default_budget=None,
            render=_skill_catalog,
            active_when=_has_auto_skills,
            activation_reason=lambda context: (
                "auto-eligible skills exist"
                if _has_auto_skills(context)
                else "no auto-eligible skills"
            ),
            source="discovered skill catalog",
            budgeter=truncate_skill_catalog_to_budget,
        ),
        PromptModule(
            name="wiki.catalog",
            description="Names and summaries of available wiki pages.",
            priority=500,
            scope="runtime",
            trust="global",
            default_budget=None,
            render=_wiki_index,
            active_when=lambda context: bool(_wiki_index(context)),
            activation_reason=lambda context: (
                "wiki is enabled and pages exist"
                if _wiki_index(context)
                else "wiki disabled or empty"
            ),
            source="global/project wiki indices",
        ),
        PromptModule(
            name="repo.map",
            description="Automatic ranked repository map.",
            priority=600,
            scope="runtime",
            trust="project",
            default_budget=None,
            render=_repo_map,
            active_when=lambda context: (
                context.config.context.repo_map == "auto"
                and context.project_root is not None
                and bool(_repo_map(context))
            ),
            activation_reason=lambda context: (
                "context.repo_map is auto"
                if context.config.context.repo_map == "auto"
                else f"context.repo_map is {context.config.context.repo_map}"
            ),
            source=lambda context: str(context.project_root or "repository"),
        ),
        PromptModule(
            name="session.date",
            description="Current local date, injected once per thread/day.",
            priority=700,
            scope="session",
            trust="global",
            default_budget=SESSION_DATE_BUDGET,
            render=lambda context: prompts.date_context(context.now),
            active_when=lambda context: True,
            activation_reason=lambda context: "first turn per thread/local day",
            source="local clock",
            runtime_eligible=False,
        ),
    ]

    # Apply configured budgets without moving configuration knowledge into each
    # renderer.  ``replace`` keeps the module definitions frozen and testable.
    budget_fields = {
        "context.project": "project_context_tokens",
        "memory.catalog": "memory_tokens",
        "skills.catalog": "skill_catalog_tokens",
        "wiki.catalog": "wiki_index_tokens",
        "repo.map": "repo_map_tokens",
    }

    # Config-dependent budgets are resolved later in ``with_context_budgets``;
    # the registry can still be constructed independently in unit tests.
    for skill in (skills or {}).values():
        module_name = f"skill.{skill.name}"

        def _render_skill(
            context: PromptModuleContext, item: Skill = skill
        ) -> str:
            return render_skill_invocation(item)

        def _skill_active(
            context: PromptModuleContext, name: str = module_name
        ) -> bool:
            return name in context.explicit_scopes

        def _skill_reason(
            context: PromptModuleContext, name: str = module_name
        ) -> str:
            if name in context.explicit_scopes:
                return f"explicit {_explicit_scope(context, name)} activation"
            return "not explicitly activated"

        def _skill_scope(
            context: PromptModuleContext, name: str = module_name
        ) -> PromptModuleScope:
            return _explicit_scope(context, name)

        modules.append(
            PromptModule(
                name=module_name,
                description=skill.description or f"Explicit {skill.name} skill body.",
                priority=450,
                scope="turn",
                trust="project" if skill.scope == "project" else "global",
                default_budget=EXPLICIT_SKILL_BUDGET,
                render=_render_skill,
                active_when=_skill_active,
                activation_reason=_skill_reason,
                source=str(skill.path) if skill.path else f"skill:{skill.name}",
                kind="skill",
                user_activatable=True,
                scope_for=_skill_scope,
            )
        )
    registry = PromptModuleRegistry(modules)
    # Stash only field names; ``with_context_budgets`` returns resolved copies.
    registry._budget_fields = budget_fields
    return registry


def with_context_budgets(
    registry: PromptModuleRegistry,
    context: PromptModuleContext,
) -> PromptModuleRegistry:
    """Resolve config-owned default budgets onto immutable module definitions."""
    fields = registry._budget_fields
    modules = [
        replace(
            module,
            default_budget=getattr(context.config.context, fields[module.name]),
        )
        if module.name in fields
        else module
        for module in registry.modules
    ]
    return PromptModuleRegistry(modules)


def prompt_module_diagnostics(
    registry: PromptModuleRegistry,
    context: PromptModuleContext,
    assembly: PromptAssembly | None = None,
) -> dict[str, Any]:
    """JSON-safe registry state for doctor/front-end diagnostics."""
    statuses = registry.statuses(context, assembly=assembly)
    return {
        "prompt_tokens": assembly.token_count if assembly is not None else None,
        "modules": [
            {
                "name": status.name,
                "active": status.active,
                "reason": status.activation_reason,
                "scope": status.scope,
                "trust": status.trust,
                "source": status.source,
                "tokens": status.token_count,
                "budget": status.configured_budget,
                "truncated": status.truncated,
                "kind": status.kind,
            }
            for status in statuses
        ],
    }
