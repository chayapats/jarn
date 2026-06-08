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


def splash(version: str, model: str | None, mode: str) -> str:
    """Big ASCII wordmark welcome. Model/mode live in the status bar."""
    return (
        f"[b {palette.ACCENT}]{WORDMARK}[/b {palette.ACCENT}]\n"
        f"[dim]{TAGLINE} · v{version}[/dim]\n\n"
        f"[dim]Type a message · [/dim][b]/help[/b][dim] for commands · "
        f"Tab complete · Shift+Tab mode · Esc cancel[/dim]"
    )
