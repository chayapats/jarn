"""Help, usage errors, and README rows — all derived from the command registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jarn.commands.registry import (
    COMMAND_SPECS,
    HELP_CLI_EQUIVALENT,
    CommandSpec,
    grouped_specs,
    help_blurb_key,
    help_description_key,
    help_group_order,
    slash_index,
    slash_usage,
    spec_by_name,
)
from jarn.tui import grammar, layout
from jarn.tui.i18n import CATALOGS, resolve_locale, t
from jarn.tui.layout import Dialect

_MODE_IDS: tuple[str, ...] = ("plan", "ask", "auto-edit", "yolo")


def _loc(locale: str | None) -> str:
    """Resolve ``en`` / ``th`` for chrome lookups.

    ``None`` is English so callers that have not wired ``ui.locale`` stay
    stable. Pass ``resolve_locale(config)`` from the REPL, or the resolved
    Telegram bot locale for HTML pages.
    """
    if locale is None:
        return "en"
    if locale == "auto":
        return resolve_locale("auto")
    if locale in CATALOGS:
        return locale
    raise ValueError(f"unknown locale {locale!r}; expected 'en' or 'th'")


def _cmd_description(spec: CommandSpec, locale: str) -> str:
    return t(help_description_key(spec.name), locale)


def _cmd_blurb(spec: CommandSpec, locale: str) -> str:
    key = help_blurb_key(spec.name)
    if key in CATALOGS[locale]:
        return t(key, locale)
    return _cmd_description(spec, locale)


def format_help(
    custom: dict[str, Any] | None = None,
    *,
    custom_description: Callable[[Any], str] | None = None,
    dialect: Dialect = "rich",
    columns: int | None = None,
    locale: str | None = None,
) -> str:
    """Build ``/help`` body, grouped by section.

    ``dialect='html'`` is Telegram ``parse_mode=HTML`` (same catalog, no Rich).
    ``columns=None`` (default) is the wide single-line layout; do not probe
    the terminal here. Pass a width below ``grammar.HELP_NARROW_COLUMNS`` to
    wrap each command onto two lines.
    """
    loc = _loc(locale)
    lines: list[str] = [
        layout.title(t("help.title", loc), dialect=dialect),
        layout.muted(t("help.subtitle", loc), dialect=dialect),
        "",
    ]

    grouped = grouped_specs()
    for group_name in help_group_order():
        specs = [s for s in grouped.get(group_name, []) if s.index]
        if not specs:
            continue
        lines.append(layout.title(t(f"help.group.{group_name}", loc), dialect=dialect))
        name_width = min(
            grammar.HELP_NAME_WIDTH,
            max(len(slash_index(spec)) for spec in specs),
        )
        for spec in specs:
            lines.append(
                layout.row(
                    slash_index(spec),
                    _cmd_description(spec, loc),
                    name_width=name_width,
                    dialect=dialect,
                    columns=columns,
                )
            )
        lines.append("")

    if custom:
        lines.append(layout.title(t("help.group.project", loc), dialect=dialect))
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

    lines.append(layout.title(t("help.group.shortcuts", loc), dialect=dialect))
    lines.append(f"  {layout.muted(grammar.shortcut_line(), dialect=dialect)}")
    lines.append(f"  {layout.muted(t('help.copy_hint', loc), dialect=dialect)}")
    return "\n".join(lines).rstrip() + "\n"


def _glyphs_page(*, dialect: Dialect, locale: str) -> str:
    """Contributor legend. Not part of the default ``/help`` index."""
    return (
        "\n".join(
            [
                layout.title(t("help.glyphs.title", locale), dialect=dialect),
                "",
                f"  {layout.muted(grammar.glyph_legend(), dialect=dialect)}",
            ]
        ).rstrip()
        + "\n"
    )


def format_help_detail(
    name: str,
    custom: dict[str, Any] | None = None,
    *,
    custom_description: Callable[[Any], str] | None = None,
    dialect: Dialect = "rich",
    locale: str | None = None,
) -> str:
    """``/help <name>`` page from the registry (or a custom command)."""
    loc = _loc(locale)
    topic = name.strip().lstrip("/").lower().replace("-", "_")
    if topic == "glyphs":
        return _glyphs_page(dialect=dialect, locale=loc)
    spec = spec_by_name(name)
    if spec is not None:
        return _detail_from_spec(spec, dialect=dialect, locale=loc)
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
                    layout.muted(t("help.custom_note", loc), dialect=dialect),
                ]
            )
    return unknown_command(name, dialect=dialect, locale=loc)


def _mode_gloss_lines(locale: str, dialect: Dialect) -> list[str]:
    width = max(len(mode_id) for mode_id in _MODE_IDS)
    lines = [""]
    for mode_id in _MODE_IDS:
        lines.append(
            layout.row(
                mode_id,
                t(f"help.mode.{mode_id}", locale),
                name_width=width,
                dialect=dialect,
            )
        )
    return lines


def _detail_from_spec(
    spec: CommandSpec, *, dialect: Dialect = "rich", locale: str = "en"
) -> str:
    lines = [layout.title(slash_usage(spec), dialect=dialect), ""]
    lines.append(f"  {layout.escape(_cmd_blurb(spec, locale), dialect=dialect)}")
    if spec.name == "mode":
        lines.extend(_mode_gloss_lines(locale, dialect))
    cli = HELP_CLI_EQUIVALENT.get(spec.name)
    if cli:
        lines.append("")
        lines.append(
            layout.muted(t("help.cli_equivalent", locale, command=cli), dialect=dialect)
        )
    if spec.alias_of:
        lines.append("")
        lines.append(
            layout.muted(t("help.alias_of", locale, name=spec.alias_of), dialect=dialect)
        )
    if spec.aliases:
        names = ", ".join(f"/{a}" for a in spec.aliases)
        lines.append("")
        lines.append(
            layout.muted(
                t("help.also_aliases", locale, name=spec.name, aliases=names),
                dialect=dialect,
            )
        )
    lines.append("")
    lines.append(
        layout.kv(t("help.usage_label", locale), slash_usage(spec), dialect=dialect)
    )
    if spec.examples:
        lines.append("")
        lines.append(layout.muted(t("help.examples", locale), dialect=dialect))
        for item in spec.examples:
            lines.append(f"  {layout.accent(item, dialect=dialect)}")
    if spec.related:
        rel = "  ".join(f"/{item}" for item in spec.related)
        lines.append("")
        lines.append(layout.kv(t("help.related", locale), rel, dialect=dialect))
    return "\n".join(lines)


def usage_error(
    name: str,
    *,
    extra: str = "",
    hint: str = "",
    dialect: Dialect = "rich",
    locale: str | None = None,
) -> str:
    """Standard failed-command page. Syntax always comes from the registry."""
    loc = _loc(locale)
    spec = spec_by_name(name)
    syntax = slash_usage(spec) if spec is not None else f"/{name}"
    examples = spec.examples if spec is not None else ()
    related = spec.related if spec is not None else ()
    lines: list[str] = []
    usage_line = t("help.usage", loc, syntax=syntax)
    if extra:
        lines.append(layout.err(extra, dialect=dialect))
        lines.append(layout.muted(usage_line, dialect=dialect))
    else:
        lines.append(layout.err(usage_line, dialect=dialect))
    if hint:
        lines.append(layout.muted(hint, dialect=dialect))
    if examples:
        lines.append("")
        lines.append(layout.muted(t("help.examples", loc), dialect=dialect))
        for item in examples:
            lines.append(f"  {layout.accent(item, dialect=dialect)}")
    if related:
        rel = "  ".join(f"/{item}" for item in related)
        lines.append("")
        lines.append(
            layout.muted(f"{t('help.related', loc)}  {rel}", dialect=dialect)
        )
    topic = spec.name if spec is not None else name
    lines.append("")
    lines.append(layout.muted(t("help.details_hint", loc, topic=topic), dialect=dialect))
    return "\n".join(lines)


def unknown_command(
    name: str,
    suggestions: list[str] | None = None,
    *,
    dialect: Dialect = "rich",
    locale: str | None = None,
) -> str:
    import difflib

    from jarn.commands.registry import _SPEC_BY_NAME

    loc = _loc(locale)
    clean = name.lstrip("/")
    if suggestions is None:
        suggestions = difflib.get_close_matches(
            clean.replace("-", "_").lower(),
            list(_SPEC_BY_NAME),
            n=5,
            cutoff=0.55,
        )
    lines = [layout.err(t("help.unknown", loc, name=clean), dialect=dialect), ""]
    if suggestions:
        shown = []
        for item in suggestions:
            spec = spec_by_name(item)
            label = spec.name if spec is not None else item.replace("_", "-")
            if label not in shown:
                shown.append(label)
        lines.append(
            "  "
            + t("help.did_you_mean", loc)
            + "  "
            + "  ".join(layout.accent(f"/{item}", dialect=dialect) for item in shown)
            + "?"
        )
        lines.append("")
    lines.append(layout.muted(t("help.list_hint", loc), dialect=dialect))
    return "\n".join(lines)


def readme_command_rows() -> list[tuple[str, str]]:
    """(command cell, description) rows generated from the registry.

    Alias-only specs are omitted: the primary row's description names them.
    English registry copy is the ``/help`` SSOT — not the locale catalog.
    """
    rows: list[tuple[str, str]] = []
    for spec in COMMAND_SPECS:
        if spec.alias_of:
            continue
        cell = f"`{slash_usage(spec)}`"
        rows.append((cell, spec.description))
    return rows
