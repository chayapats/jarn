"""Textual themes for J.A.R.N. — generated from :mod:`jarn.tui.palette`.

Three variants: dark (default), light, and high-contrast. Returned as Textual
``Theme`` objects registered on the app. Rich / prompt_toolkit colours come from
the same ``_PALETTES`` rows via :func:`~jarn.tui.palette.configure_ui`.
"""

from __future__ import annotations

from textual.theme import Theme

from jarn.tui.palette import _PALETTES, ThemeName

_CONFIG_THEME_MAP: dict[str, ThemeName] = {
    "dark": "dark",
    "light": "light",
    "high-contrast": "high-contrast",
}

_THEME_NAMES: dict[ThemeName, str] = {
    "dark": "jarn-dark",
    "light": "jarn-light",
    "high-contrast": "jarn-high-contrast",
}


def _build_theme(key: ThemeName) -> Theme:
    p = _PALETTES[key]
    t = p.textual
    return Theme(
        name=_THEME_NAMES[key],
        primary=t.primary,
        secondary=t.secondary,
        accent=p.accent,
        foreground=p.toolbar_fg,
        background=t.background,
        surface=t.surface,
        panel=t.panel,
        success=p.c_success,
        warning=p.c_warn,
        error=p.c_error,
        dark=t.dark,
    )


JARN_DARK = _build_theme("dark")
JARN_LIGHT = _build_theme("light")
JARN_HIGH_CONTRAST = _build_theme("high-contrast")

ALL_THEMES = {t.name: t for t in (JARN_DARK, JARN_LIGHT, JARN_HIGH_CONTRAST)}

#: Map the short config value to a registered theme name.
CONFIG_THEME_MAP = {k: _THEME_NAMES[v] for k, v in _CONFIG_THEME_MAP.items()}


def theme_name_for(config_value: str) -> str:
    key = _CONFIG_THEME_MAP.get(config_value, "dark")
    return _THEME_NAMES[key]
