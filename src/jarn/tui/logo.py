"""ASCII wordmark and splash text for J.A.R.N."""

from __future__ import annotations

from pathlib import Path

from jarn.tui import grammar, layout

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
    skills: int | None = None,
) -> str:
    """Orientation strip under the wordmark (or alone when splash is off)."""
    from jarn.permissions.labels import permission_mode_name

    glyph = grammar.MODE_GLYPH.get(mode, grammar.MODE_GLYPH["ask"])
    label = permission_mode_name(mode)
    if skills is None:
        skills_text = "type /skills"
    elif skills == 0:
        skills_text = "none loaded   ·  type /skills"
    else:
        skills_text = f"{skills} loaded   ·  type /skills"
    return "\n".join(
        [
            layout.kv("Model", model),
            layout.kv("Folder", folder),
            layout.kv("Mode", f"{glyph} {label}"),
            layout.kv("Skills", skills_text),
        ]
    )


def splash(version: str) -> str:
    """Big ASCII wordmark welcome."""
    return (
        f"{layout.accent(WORDMARK, bold=True)}\n"
        f"{layout.muted(f'{TAGLINE} · v{version}')}\n\n"
        f"{SHORTCUT_HINT}"
    )


def splash_compact(version: str) -> str:
    """Single-line wordmark + version + shortcut hint."""
    return (
        f"{layout.accent('JARN', bold=True)} "
        f"{layout.muted(f'v{version} · {TAGLINE}')}  "
        f"{SHORTCUT_HINT}"
    )
