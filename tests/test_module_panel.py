"""Interactive prompt-module panel state and rendering."""

from __future__ import annotations

from dataclasses import replace

from jarn.agent.prompt_modules import PromptModuleScope, PromptModuleStatus
from jarn.controller import CommandResult
from jarn.repl.module_panel import ModulePanel


def _status(
    name: str,
    *,
    active: bool = False,
    scope: PromptModuleScope = "runtime",
    user_activatable: bool = False,
    description: str = "Short explanation.",
) -> PromptModuleStatus:
    return PromptModuleStatus(
        name=name,
        description=description,
        active=active,
        activation_reason="test reason",
        scope=scope,
        trust="global",
        source="test",
        token_count=12 if active else 0,
        configured_budget=100,
        truncated=False,
        kind="skill" if user_activatable else "builtin",
        user_activatable=user_activatable,
    )


def _text(panel: ModulePanel) -> str:
    return "".join(text for _style, text in panel.render_lines())


def test_panel_starts_on_first_actionable_skill_and_explains_it() -> None:
    statuses = (
        _status("mode.plan", active=True),
        _status(
            "skill.audit",
            user_activatable=True,
            description="Check changes for security and correctness.",
        ),
    )
    panel = ModulePanel(
        get_statuses=lambda: statuses,
        activate=lambda _name, _scope: CommandResult("unused"),
        deactivate=lambda _name: CommandResult("unused"),
    )

    assert panel.current().name == "skill.audit"
    rendered = _text(panel)
    assert "AUTOMATIC" in rendered
    assert "OPTIONAL SKILLS" in rendered
    assert "Check changes for security and correctness." in rendered
    assert "Space/Enter next turn" in rendered


def test_panel_toggle_enables_next_turn_then_turns_off() -> None:
    current = _status("skill.audit", user_activatable=True)
    calls: list[tuple[str, str]] = []

    def get_statuses() -> tuple[PromptModuleStatus, ...]:
        return (current,)

    def activate(name: str, scope: PromptModuleScope) -> CommandResult:
        nonlocal current
        calls.append((name, scope))
        current = replace(current, active=True, scope=scope)
        return CommandResult(f"Activated {name} for the next turn.")

    def deactivate(name: str) -> CommandResult:
        nonlocal current
        calls.append((name, "off"))
        current = replace(current, active=False, scope="turn")
        return CommandResult(f"Deactivated {name}.")

    panel = ModulePanel(
        get_statuses=get_statuses,
        activate=activate,
        deactivate=deactivate,
    )
    panel.toggle_turn()
    assert calls[-1] == ("skill.audit", "turn")
    assert "● Next turn" in _text(panel)

    panel.toggle_turn()
    assert calls[-1] == ("skill.audit", "off")
    assert "○ Off" in _text(panel)


def test_panel_can_keep_skill_for_session_and_disable_it() -> None:
    current = _status("skill.review", user_activatable=True)

    def activate(name: str, scope: PromptModuleScope) -> CommandResult:
        nonlocal current
        current = replace(current, active=True, scope=scope)
        return CommandResult(f"Activated {name} for thread abcdef12.")

    def deactivate(name: str) -> CommandResult:
        nonlocal current
        current = replace(current, active=False, scope="turn")
        return CommandResult(f"Deactivated {name}.")

    panel = ModulePanel(
        get_statuses=lambda: (current,),
        activate=activate,
        deactivate=deactivate,
    )
    panel.enable_session()
    assert "● Session" in _text(panel)

    panel.disable()
    assert "○ Off" in _text(panel)


def test_panel_keeps_automatic_modules_read_only_with_plain_explanation() -> None:
    calls: list[str] = []
    panel = ModulePanel(
        get_statuses=lambda: (_status("repo.map", active=True),),
        activate=lambda _name, _scope: calls.append("activate"),
        deactivate=lambda _name: calls.append("deactivate"),
    )

    panel.toggle_turn()
    assert calls == []
    rendered = _text(panel)
    assert "Repository map" in rendered
    assert "Managed automatically" in rendered
    assert "is automatic" in rendered


def test_panel_uses_bounded_viewport_for_large_skill_catalog() -> None:
    statuses = tuple(
        _status(f"skill.skill-{index}", user_activatable=True)
        for index in range(20)
    )
    panel = ModulePanel(
        get_statuses=lambda: statuses,
        activate=lambda _name, _scope: CommandResult("unused"),
        deactivate=lambda _name: CommandResult("unused"),
        max_visible=5,
    )
    panel.move(10)

    rendered = _text(panel)
    assert "↑ " in rendered and "↓ " in rendered
    assert sum(f"skill-{index}" in rendered for index in range(20)) < 20
