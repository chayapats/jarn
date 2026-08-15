"""Framework-agnostic state model for the interactive prompt-module picker."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jarn.agent.prompt_modules import PromptModuleScope, PromptModuleStatus
from jarn.tui import grammar, palette

_FRIENDLY_NAMES = {
    "mode.plan": "Plan guidance",
    "context.project": "Project guide",
    "memory.catalog": "Memory index",
    "skills.catalog": "Skill catalog",
    "wiki.catalog": "Wiki index",
    "repo.map": "Repository map",
    "session.date": "Current date",
}

_FRIENDLY_DESCRIPTIONS = {
    "mode.plan": "Adds read-only planning guidance while Plan mode is active.",
    "context.project": "Adds trusted instructions from this project's context file.",
    "memory.catalog": "Lists relevant saved memories so the agent knows what it can recall.",
    "skills.catalog": "Lists available skills without loading their full instructions.",
    "wiki.catalog": "Lists available wiki pages and their short summaries.",
    "repo.map": "Adds a compact map of important files and symbols in the repository.",
    "session.date": "Tells the agent today's local date once per conversation day.",
}


class ModulePanel:
    """Interactive view/controller for prompt modules.

    Automatic context modules are deliberately read-only: their activation is
    derived from mode, trust, configuration, and available content.  Explicit
    skill bodies can be enabled for the next turn or the current thread.
    """

    def __init__(
        self,
        *,
        get_statuses: Callable[[], tuple[PromptModuleStatus, ...]],
        activate: Callable[[str, PromptModuleScope], Any],
        deactivate: Callable[[str], Any],
        max_visible: int = 10,
    ) -> None:
        self._get_statuses = get_statuses
        self._activate = activate
        self._deactivate = deactivate
        self.max_visible = max(3, max_visible)
        self.statuses: list[PromptModuleStatus] = []
        self.item_index = 0
        self.message = ""
        self.message_ok = True
        self.refresh()

    def refresh(self) -> None:
        """Reload live state while preserving the selected module by name."""
        selected = self.current().name if self.statuses else None
        statuses = list(self._get_statuses())
        automatic = [status for status in statuses if not status.user_activatable]
        optional = sorted(
            (status for status in statuses if status.user_activatable),
            key=lambda status: status.name.lower(),
        )
        self.statuses = automatic + optional
        if not self.statuses:
            self.item_index = 0
            return
        if selected is not None:
            self.item_index = next(
                (
                    index
                    for index, status in enumerate(self.statuses)
                    if status.name == selected
                ),
                min(self.item_index, len(self.statuses) - 1),
            )
        else:
            # Put the initial selection on the first switchable skill when one
            # exists; opening the picker should land on something actionable.
            self.item_index = next(
                (
                    index
                    for index, status in enumerate(self.statuses)
                    if status.user_activatable
                ),
                0,
            )

    def current(self) -> PromptModuleStatus:
        return self.statuses[self.item_index]

    def move(self, delta: int) -> None:
        if self.statuses:
            self.item_index = (self.item_index + delta) % len(self.statuses)
            self.message = ""

    def toggle_turn(self) -> None:
        """Space/Enter: turn an optional module off or enable it once."""
        if not self.statuses:
            return
        status = self.current()
        if not status.user_activatable:
            self._locked_message(status)
            return
        result = (
            self._deactivate(status.name)
            if status.active
            else self._activate(status.name, "turn")
        )
        self._finish_action(result)

    def enable_session(self) -> None:
        """Keep the selected optional module active for the current thread."""
        if not self.statuses:
            return
        status = self.current()
        if not status.user_activatable:
            self._locked_message(status)
            return
        self._finish_action(self._activate(status.name, "session"))

    def disable(self) -> None:
        """Remove explicit activation from the selected optional module."""
        if not self.statuses:
            return
        status = self.current()
        if not status.user_activatable:
            self._locked_message(status)
            return
        if not status.active:
            self.message = f"{self._label(status)} is already off."
            self.message_ok = True
            return
        self._finish_action(self._deactivate(status.name))

    def _locked_message(self, status: PromptModuleStatus) -> None:
        self.message = (
            f"{self._label(status)} is automatic; its state follows mode, "
            "trust, configuration, and available context."
        )
        self.message_ok = False

    def _finish_action(self, result: Any) -> None:
        text = str(getattr(result, "text", result))
        self.message = text
        self.message_ok = text.startswith(("Activated ", "Deactivated "))
        self.refresh()

    @staticmethod
    def _label(status: PromptModuleStatus) -> str:
        if status.name.startswith("skill."):
            label = status.name.removeprefix("skill.")
        else:
            label = _FRIENDLY_NAMES.get(status.name, status.name)
        return ModulePanel._short(label, 30)

    @staticmethod
    def _description(status: PromptModuleStatus) -> str:
        text = _FRIENDLY_DESCRIPTIONS.get(status.name, status.description)
        return ModulePanel._short(text, 140)

    @staticmethod
    def _short(text: str, limit: int) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1].rstrip() + "…"

    @staticmethod
    def _state(status: PromptModuleStatus) -> tuple[str, str]:
        if status.user_activatable:
            if not status.active:
                return palette.C_DIM, "○ Off"
            if status.scope == "session":
                return palette.C_SUCCESS, "● Session"
            return palette.C_SUCCESS, "● Next turn"
        if status.active:
            return palette.C_SUCCESS, "● Auto on"
        return palette.C_DIM, "○ Auto off"

    def _visible_range(self) -> tuple[int, int]:
        total = len(self.statuses)
        if total <= self.max_visible:
            return 0, total
        half = self.max_visible // 2
        start = max(0, self.item_index - half)
        start = min(start, total - self.max_visible)
        return start, start + self.max_visible

    def render_lines(self) -> list[tuple[str, str]]:
        """Return prompt_toolkit fragments for the in-app panel."""
        active = sum(status.active for status in self.statuses)
        optional = sum(status.user_activatable for status in self.statuses)
        out: list[tuple[str, str]] = [
            ("bold", "  ◫  Prompt modules"),
            (palette.C_DIM, "   esc/q to close\n"),
            (
                palette.C_DIM,
                f"   {active} active · {optional} optional skill"
                f"{'s' if optional != 1 else ''}\n\n",
            ),
        ]
        if not self.statuses:
            out.append((palette.C_DIM, "   No prompt modules are available.\n"))
            return out

        start, end = self._visible_range()
        if start:
            out.append((palette.C_DIM, f"   ↑ {start} more\n"))

        visible = self.statuses[start:end]
        previous_kind: str | None = None
        label_width = max((len(self._label(status)) for status in visible), default=0)
        for absolute_index, status in enumerate(visible, start=start):
            kind = "Optional skills" if status.user_activatable else "Automatic"
            if kind != previous_kind:
                out.append((palette.ACCENT, f"   {kind.upper()}\n"))
                previous_kind = kind
            selected = absolute_index == self.item_index
            label = self._label(status).ljust(label_width)
            marker = "▸ " if selected else "  "
            state_style, state_text = self._state(status)
            if selected:
                out.append(("reverse", f"   {marker}{label}   {state_text} \n"))
            else:
                out.append(("", f"   {marker}{label}   "))
                out.append((state_style, state_text))
                out.append(("", "\n"))

        hidden = len(self.statuses) - end
        if hidden:
            out.append((palette.C_DIM, f"   ↓ {hidden} more\n"))

        status = self.current()
        out.append((palette.C_DIM, "\n   " + "─" * 58 + "\n"))
        out.append(("bold", f"   {self._label(status)}  "))
        out.append((palette.C_DIM, f"{status.name}\n"))
        out.append(("", f"   {self._description(status)}\n"))
        out.append(
            (palette.C_DIM, f"   Why: {self._short(status.activation_reason, 120)}\n")
        )
        if status.configured_budget is not None:
            out.append(
                (
                    palette.C_DIM,
                    f"   Prompt size: {status.token_count:,}/"
                    f"{status.configured_budget:,} tokens\n",
                )
            )
        if status.user_activatable:
            out.append(
                (
                    palette.ACCENT,
                    "   Space/Enter next turn or off · s keep for session · x off\n",
                )
            )
        else:
            out.append((palette.C_DIM, "   Managed automatically · cannot be toggled here\n"))
        out.append((palette.C_DIM, "   ↑/↓ move · Esc/q close\n"))
        if self.message:
            style, glyph = (palette.C_SUCCESS, grammar.GLYPH_OK) if self.message_ok else (palette.C_ERROR, grammar.GLYPH_FAIL)
            out.append((style, f"   {glyph} {self.message}\n"))
        return out
