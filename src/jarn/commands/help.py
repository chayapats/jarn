"""Help, usage errors, and README rows — all derived from the command registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

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
from jarn.tui.layout import Dialect


def format_help(
    custom: dict[str, Any] | None = None,
    *,
    custom_description: Callable[[Any], str] | None = None,
    dialect: Dialect = "rich",
    columns: int | None = None,
) -> str:
    """Build ``/help`` body, grouped by section.

    ``dialect='html'`` is Telegram ``parse_mode=HTML`` (same catalog, no Rich).
    ``columns=None`` (default) is the wide single-line layout; do not probe
    the terminal here. Pass a width below ``grammar.HELP_NARROW_COLUMNS`` to
    wrap each command onto two lines.
    """
    lines: list[str] = [
        layout.title("Commands", dialect=dialect),
        layout.muted("type /help <name> for details", dialect=dialect),
        "",
    ]

    grouped = grouped_specs()
    for group_name in help_group_order():
        specs = [s for s in grouped.get(group_name, []) if s.index]
        if not specs:
            continue
        lines.append(layout.title(group_name, dialect=dialect))
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
                    dialect=dialect,
                    columns=columns,
                )
            )
        lines.append("")

    if custom:
        lines.append(layout.title("Project commands", dialect=dialect))
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
            lines.append(
                layout.row(name, desc, name_width=width, dialect=dialect, columns=columns)
            )
        lines.append("")

    lines.append(layout.title("Shortcuts", dialect=dialect))
    lines.append(f"  {layout.muted(grammar.shortcut_line(), dialect=dialect)}")
    lines.append(f"  {layout.muted(grammar.HELP_COPY_HINT, dialect=dialect)}")
    lines.append("")
    lines.append(layout.title("Glyphs", dialect=dialect))
    lines.append(f"  {layout.muted(grammar.glyph_legend(), dialect=dialect)}")
    return "\n".join(lines).rstrip() + "\n"


def format_help_detail(
    name: str,
    custom: dict[str, Any] | None = None,
    *,
    custom_description: Callable[[Any], str] | None = None,
    dialect: Dialect = "rich",
) -> str:
    """``/help <name>`` page from the registry (or a custom command)."""
    spec = spec_by_name(name)
    if spec is not None:
        return _detail_from_spec(spec, dialect=dialect)
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
                    layout.title(f"/{getattr(command, 'name', key)}", dialect=dialect),
                    "",
                    f"  {layout.escape(desc, dialect=dialect)}",
                    "",
                    layout.muted(
                        "Project command — its body is sent to the agent.",
                        dialect=dialect,
                    ),
                ]
            )
    return unknown_command(name, dialect=dialect)


def _detail_from_spec(spec: CommandSpec, *, dialect: Dialect = "rich") -> str:
    lines = [layout.title(slash_usage(spec), dialect=dialect), ""]
    lines.append(f"  {layout.escape(spec.blurb or spec.description, dialect=dialect)}")
    if spec.alias_of:
        lines.append("")
        lines.append(layout.muted(f"Alias of /{spec.alias_of}.", dialect=dialect))
    if spec.aliases:
        names = ", ".join(f"/{a}" for a in spec.aliases)
        lines.append("")
        lines.append(layout.muted(f"Also /{spec.name}: {names}", dialect=dialect))
    lines.append("")
    lines.append(layout.kv("Usage", slash_usage(spec), dialect=dialect))
    if spec.examples:
        lines.append("")
        lines.append(layout.muted("Examples", dialect=dialect))
        for item in spec.examples:
            lines.append(f"  {layout.accent(item, dialect=dialect)}")
    if spec.related:
        rel = "  ".join(f"/{item}" for item in spec.related)
        lines.append("")
        lines.append(layout.kv("Related", rel, dialect=dialect))
    return "\n".join(lines)


def usage_error(
    name: str,
    *,
    extra: str = "",
    hint: str = "",
    dialect: Dialect = "rich",
) -> str:
    """Standard failed-command page. Syntax always comes from the registry."""
    spec = spec_by_name(name)
    syntax = slash_usage(spec) if spec is not None else f"/{name}"
    examples = spec.examples if spec is not None else ()
    related = spec.related if spec is not None else ()
    lines: list[str] = []
    if extra:
        lines.append(layout.err(extra, dialect=dialect))
        lines.append(layout.muted(f"Usage: {syntax}", dialect=dialect))
    else:
        lines.append(layout.err(f"Usage: {syntax}", dialect=dialect))
    if hint:
        lines.append(layout.muted(hint, dialect=dialect))
    if examples:
        lines.append("")
        lines.append(layout.muted("Examples", dialect=dialect))
        for item in examples:
            lines.append(f"  {layout.accent(item, dialect=dialect)}")
    if related:
        rel = "  ".join(f"/{item}" for item in related)
        lines.append("")
        lines.append(layout.muted(f"Related  {rel}", dialect=dialect))
    topic = spec.name if spec is not None else name
    lines.append("")
    lines.append(layout.muted(f"Type /help {topic} for details.", dialect=dialect))
    return "\n".join(lines)


def unknown_command(
    name: str,
    suggestions: list[str] | None = None,
    *,
    dialect: Dialect = "rich",
) -> str:
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
    lines = [layout.err(f"Unknown command: /{clean}", dialect=dialect), ""]
    if suggestions:
        shown = []
        for item in suggestions:
            spec = spec_by_name(item)
            label = spec.name if spec is not None else item.replace("_", "-")
            if label not in shown:
                shown.append(label)
        lines.append(
            "  Did you mean  "
            + "  ".join(layout.accent(f"/{item}", dialect=dialect) for item in shown)
            + "?"
        )
        lines.append("")
    lines.append(layout.muted("Type /help to list commands.", dialect=dialect))
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
