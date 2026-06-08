"""Textual themes for J.A.R.N. — calm cyan/teal "reliable" accent.

Three variants: dark (default), light, and high-contrast. Returned as Textual
``Theme`` objects registered on the app.
"""

from __future__ import annotations

from textual.theme import Theme

JARN_DARK = Theme(
    name="jarn-dark",
    primary="#16b8a6",      # teal
    secondary="#0891b2",    # cyan
    accent="#22d3ee",
    foreground="#e8f0f0",
    background="#0b1416",
    surface="#10201f",
    panel="#1c3336",
    success="#22c55e",
    warning="#ffb454",
    error="#ff6b6b",
    dark=True,
)

JARN_LIGHT = Theme(
    name="jarn-light",
    primary="#0d9488",
    secondary="#0e7490",
    accent="#0891b2",
    foreground="#0b1416",
    background="#f6fafa",
    surface="#ffffff",
    panel="#e6f2f1",
    success="#15803d",
    warning="#ffb454",
    error="#ff6b6b",
    dark=False,
)

JARN_HIGH_CONTRAST = Theme(
    name="jarn-high-contrast",
    primary="#00ffe1",
    secondary="#00e5ff",
    accent="#ffffff",
    foreground="#ffffff",
    background="#000000",
    surface="#0a0a0a",
    panel="#141414",
    success="#00ff66",
    warning="#ffb454",
    error="#ff6b6b",
    dark=True,
)

ALL_THEMES = {t.name: t for t in (JARN_DARK, JARN_LIGHT, JARN_HIGH_CONTRAST)}

#: Map the short config value to a registered theme name.
CONFIG_THEME_MAP = {
    "dark": "jarn-dark",
    "light": "jarn-light",
    "high-contrast": "jarn-high-contrast",
}


def theme_name_for(config_value: str) -> str:
    return CONFIG_THEME_MAP.get(config_value, "jarn-dark")
