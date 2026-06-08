"""A tiny key-inspector TUI (``jarn keys``).

Press any key (incl. Caps Lock / language switch) and it shows exactly what the
terminal sends — the Textual key name, the character, and aliases. Use this to
diagnose terminal quirks like Caps Lock leaking a character on macOS, then share
the line with a maintainer so a precise filter can be added.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import RichLog, Static

from jarn.tui.theme import ALL_THEMES, theme_name_for


class KeyInspector(App):
    CSS = """
    Screen { background: $background; }
    #hdr { dock: top; height: auto; padding: 1 2; color: $accent; text-style: bold; }
    RichLog { padding: 0 2; }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            "Key inspector — press any key (try Caps Lock / your language switch).\n"
            "[dim]Each line shows: key=<name> char=<character> aliases=<...>. "
            "Press Ctrl+Q to quit.[/dim]",
            id="hdr",
        )
        yield RichLog(highlight=False, markup=True, id="log")

    def on_mount(self) -> None:
        for theme in ALL_THEMES.values():
            self.register_theme(theme)
        self.theme = theme_name_for("dark")

    def on_key(self, event) -> None:
        if event.key in ("ctrl+q",):
            self.exit()
            return
        char = getattr(event, "character", None)
        aliases = getattr(event, "aliases", None)
        self.query_one("#log", RichLog).write(
            f"key=[b cyan]{event.key!r}[/b cyan]  "
            f"char=[yellow]{char!r}[/yellow]  "
            f"aliases=[dim]{aliases!r}[/dim]"
        )


def run_key_inspector() -> None:
    KeyInspector().run()
