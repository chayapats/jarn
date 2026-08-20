"""Stable internal permission modes and plain-language display names."""

from __future__ import annotations

PERMISSION_MODE_NAMES: dict[str, str] = {
    "plan": "Review only",
    "ask": "Ask before changes",
    "auto-edit": "Edit workspace",
    "yolo": "Full access",
}

PERMISSION_MODE_DESCRIPTIONS: dict[str, str] = {
    "plan": "read and plan without changing files",
    "ask": "confirm changes, commands, and external actions (recommended)",
    "auto-edit": "edit workspace files; confirm commands and external actions",
    "yolo": "skip routine prompts; hard safety blocks still apply",
}


def permission_mode_name(value: str, locale: str | None = None) -> str:
    """Return a user-facing name while preserving unknown future values.

    ``locale`` is chrome only (``en`` / ``th``). ``None`` keeps the English
    display names so toolbars and onboarding that have not passed a locale
    stay stable. Mode **ids** are never translated.
    """
    if locale in {"en", "th"}:
        from jarn.tui.i18n import CATALOGS, t

        key = f"mode.label.{value}"
        if key in CATALOGS[locale]:
            return t(key, locale)
    return PERMISSION_MODE_NAMES.get(value, value)


def permission_mode_summary(
    value: str, *, include_internal: bool = False, locale: str | None = None
) -> str:
    """Return a concise display label, optionally with the stable config value."""
    name = permission_mode_name(value, locale)
    if include_internal and name != value:
        return f"{name} ({value})"
    return name
