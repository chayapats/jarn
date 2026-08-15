"""Help, usage errors, and README rows — all derived from the command registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.markup import escape as _escape_rich

from jarn.commands.registry import (
    COMMAND_SPECS,
    CommandSpec,
    grouped_specs,
    help_group_order,
    slash_index,
    slash_usage,
    spec_by_name,
)
from jarn.tui import grammar, layout


def format_help(
    custom: dict[str, Any] | None = None,
    *,
    custom_description: Callable[[Any], str] | None = None,
) -> str:
    """Build ``/help`` body (Rich markup), grouped by section."""
    lines: list[str] = [
        layout.title("Commands"),
        layout.muted("type /help <name> for details"),
        "",
    ]

    grouped = grouped_specs()
    for group_name in help_group_order():
        specs = [s for s in grouped.get(group_name, []) if s.index]
        if not specs:
            continue
        lines.append(layout.title(group_name))
        name_width = min(
            grammar.HELP_NAME_WIDTH,
            max(len(slash_index(spec)) for spec in specs),
        )
        for spec in specs:
            lines.append(
                layout.row(
                    slash_index(spec),
                    spec.description,
                    name_width=name_width,
                )
            )
        lines.append("")

    if custom:
        lines.append(layout.title("Project commands"))
        custom_rows = []
        for command in custom.values():
            name = getattr(command, "name", "")
            desc = (
                custom_description(command)
                if custom_description is not None
                else getattr(command, "description", "")
            )
            custom_rows.append((f"/{name}", desc))
        width = min(
            grammar.HELP_NAME_WIDTH,
            max((len(n) for n, _ in custom_rows), default=8),
        )
        for name, desc in custom_rows:
            lines.append(layout.row(name, desc, name_width=width))
        lines.append("")

    lines.append(layout.title("Shortcuts"))
    lines.append(f"  {layout.muted(grammar.shortcut_line())}")
    lines.append(f"  {layout.muted(grammar.HELP_COPY_HINT)}")
    lines.append("")
    lines.append(layout.title("Glyphs"))
    lines.append(f"  {layout.muted(grammar.glyph_legend())}")
    return "\n".join(lines).rstrip() + "\n"


def format_help_detail(
    name: str,
    custom: dict[str, Any] | None = None,
    *,
    custom_description: Callable[[Any], str] | None = None,
) -> str:
    """``/help <name>`` page from the registry (or a custom command)."""
    spec = spec_by_name(name)
    if spec is not None:
        return _detail_from_spec(spec)
    if custom:
        key = name.strip().lstrip("/")
        command = custom.get(key) or custom.get(key.lower())
        if command is not None:
            desc = (
                custom_description(command)
                if custom_description is not None
                else getattr(command, "description", "") or "Project command."
            )
            return "\n".join(
                [
                    layout.title(f"/{getattr(command, 'name', key)}"),
                    "",
                    f"  {_escape_rich(desc)}",
                    "",
                    layout.muted("Project command — its body is sent to the agent."),
                ]
            )
    return unknown_command(name)


def _detail_from_spec(spec: CommandSpec) -> str:
    lines = [layout.title(slash_usage(spec)), ""]
    lines.append(f"  {_escape_rich(spec.blurb or spec.description)}")
    if spec.alias_of:
        lines.append("")
        lines.append(layout.muted(f"Alias of /{spec.alias_of}."))
    if spec.aliases:
        names = ", ".join(f"/{a}" for a in spec.aliases)
        lines.append("")
        lines.append(layout.muted(f"Also /{spec.name}: {names}"))
    lines.append("")
    lines.append(layout.kv("Usage", slash_usage(spec)))
    if spec.examples:
        lines.append("")
        lines.append(layout.muted("Examples"))
        for item in spec.examples:
            lines.append(f"  {layout.accent(item)}")
    if spec.related:
        rel = "  ".join(f"/{item}" for item in spec.related)
        lines.append("")
        lines.append(layout.kv("Related", rel))
    return "\n".join(lines)


def usage_error(
    name: str,
    *,
    extra: str = "",
    hint: str = "",
) -> str:
    """Standard failed-command page. Syntax always comes from the registry."""
    spec = spec_by_name(name)
    syntax = slash_usage(spec) if spec is not None else f"/{name}"
    examples = spec.examples if spec is not None else ()
    related = spec.related if spec is not None else ()
    lines: list[str] = []
    if extra:
        lines.append(layout.err(extra))
        lines.append(layout.muted(f"Usage: {syntax}"))
    else:
        lines.append(layout.err(f"Usage: {syntax}"))
    if hint:
        lines.append(layout.muted(hint))
    if examples:
        lines.append("")
        lines.append(layout.muted("Examples"))
        for item in examples:
            lines.append(f"  {layout.accent(item)}")
    if related:
        rel = "  ".join(f"/{item}" for item in related)
        lines.append("")
        lines.append(layout.muted(f"Related  {rel}"))
    topic = spec.name if spec is not None else name
    lines.append("")
    lines.append(layout.muted(f"Type /help {topic} for details."))
    return "\n".join(lines)


def unknown_command(name: str, suggestions: list[str] | None = None) -> str:
    import difflib

    from jarn.commands.registry import _SPEC_BY_NAME

    clean = name.lstrip("/")
    if suggestions is None:
        suggestions = difflib.get_close_matches(
            clean.replace("-", "_").lower(),
            list(_SPEC_BY_NAME),
            n=5,
            cutoff=0.55,
        )
    lines = [layout.err(f"Unknown command: /{clean}"), ""]
    if suggestions:
        shown = []
        for item in suggestions:
            spec = spec_by_name(item)
            label = spec.name if spec is not None else item.replace("_", "-")
            if label not in shown:
                shown.append(label)
        lines.append(
            "  Did you mean  "
            + "  ".join(layout.accent(f"/{item}") for item in shown)
            + "?"
        )
        lines.append("")
    lines.append(layout.muted("Type /help to list commands."))
    return "\n".join(lines)


def readme_command_rows() -> list[tuple[str, str]]:
    """(command cell, description) rows for README parity tests.

    Alias-only specs are omitted: the primary row's description names them.
    """
    rows: list[tuple[str, str]] = []
    for spec in COMMAND_SPECS:
        if spec.alias_of:
            continue
        cell = f"`{slash_usage(spec)}`"
        rows.append((cell, spec.description))
    return rows
