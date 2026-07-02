"""ASCII wordmark and splash text for J.A.R.N."""

from __future__ import annotations

from jarn.tui import palette

WORDMARK = "\n".join([
    r"     ██╗      █████╗      ██████╗      ███╗   ██╗",
    r"     ██║     ██╔══██╗     ██╔══██╗     ████╗  ██║",
    r"     ██║     ███████║     ██████╔╝     ██╔██╗ ██║",
    r"██   ██║     ██╔══██║     ██╔══██╗     ██║╚██╗██║",
    r"╚█████╔╝ ██╗ ██║  ██║ ██╗ ██║  ██║ ██╗ ██║ ╚████║ ██╗",
    r" ╚════╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝ ╚═╝  ╚═╝ ╚═╝ ╚═╝  ╚═══╝ ╚═╝",
])

TAGLINE = "just a reliable nerd"


SHORTCUT_HINT = (
    "[dim]Type a message · [/dim][b]/help[/b][dim] for commands · "
    "Tab complete · Shift+Tab mode · Esc cancel[/dim]"
)


def splash(version: str) -> str:
    """Big ASCII wordmark welcome."""
    return (
        f"[b {palette.ACCENT}]{WORDMARK}[/b {palette.ACCENT}]\n"
        f"[dim]{TAGLINE} · v{version}[/dim]\n\n"
        f"{SHORTCUT_HINT}"
    )


def splash_compact(version: str) -> str:
    """Single-line wordmark + version + shortcut hint."""
    return (
        f"[b {palette.ACCENT}]JARN[/b {palette.ACCENT}] "
        f"[dim]v{version} · {TAGLINE}[/dim]  "
        f"{SHORTCUT_HINT}"
    )
