"""ASCII wordmark and splash text for J.A.R.N."""

from __future__ import annotations

from pathlib import Path

from jarn.tui import grammar, layout
from jarn.tui.i18n import t

WORDMARK = "\n".join([
    r"     ██╗      █████╗      ██████╗      ███╗   ██╗",
    r"     ██║     ██╔══██╗     ██╔══██╗     ████╗  ██║",
    r"     ██║     ███████║     ██████╔╝     ██╔██╗ ██║",
    r"██   ██║     ██╔══██╗     ██╔══██╗     ██║╚██╗██║",
    r"╚█████╔╝ ██╗ ██║  ██║ ██╗ ██║  ██║ ██╗ ██║ ╚████║ ██╗",
    r" ╚════╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝ ╚═╝  ╚═╝ ╚═╝ ╚═╝  ╚═══╝ ╚═╝",
])

TAGLINE = "just a reliable nerd"


SHORTCUT_HINT = layout.muted(grammar.SHORTCUT_HINT)


def display_folder(path: Path | None) -> str:
    """Home-relative folder label for splash / status (``~/src/jarn``)."""
    if path is None:
        return "(none)"
    try:
        return "~/" + path.expanduser().resolve().relative_to(Path.home()).as_posix()
    except (ValueError, OSError):
        return str(path)


def splash_info_strip(
    *,
    model: str,
    folder: str,
    mode: str,
) -> str:
    """Model / folder / mode kv table — extra under the full wordmark only."""
    from jarn.permissions.labels import permission_mode_name

    glyph = grammar.MODE_GLYPH.get(mode, grammar.MODE_GLYPH["ask"])
    label = permission_mode_name(mode)
    return "\n".join(
        [
            layout.kv("Model", model),
            layout.kv("Folder", folder),
            layout.kv("Mode", f"{glyph} {label}"),
        ]
    )


def splash(version: str) -> str:
    """Big ASCII wordmark welcome."""
    return (
        f"{layout.accent(WORDMARK, bold=True)}\n"
        f"{layout.muted(f'{TAGLINE} · v{version}')}\n\n"
        f"{SHORTCUT_HINT}"
    )


def splash_compact(
    version: str,
    *,
    model: str = "",
    folder: str = "",
    mode: str = "",
    locale: str | None = None,
) -> str:
    """Compact first frame: name + version, session facts, locale orientation."""
    lines = [
        f"{layout.accent('jarn', bold=True)}  {layout.muted(f'v{version}')}",
    ]
    facts = [part for part in (model, folder, mode) if part]
    if facts:
        lines.append(layout.muted(f"  {'  ·  '.join(facts)}"))
    lines.append(layout.muted(f"  {t('splash.orientation', locale)}"))
    return "\n".join(lines)


def render_launch_banner(
    version: str,
    *,
    variant: str,
    first_run: bool = False,
    model: str = "",
    folder: str = "",
    mode: str = "",
    locale: str | None = None,
) -> str:
    """Compose the first-frame splash. Compact has no kv table and no skills line."""
    if first_run or variant == "full":
        return f"{splash(version)}\n{splash_info_strip(model=model, folder=folder, mode=mode)}"
    if variant == "compact":
        return splash_compact(
            version,
            model=model,
            folder=folder,
            mode=mode,
            locale=locale,
        )
    return SHORTCUT_HINT
