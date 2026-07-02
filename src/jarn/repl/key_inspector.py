"""prompt_toolkit key inspector (``jarn keys --repl``).

Press any key (incl. Caps Lock / language switch) and it logs exactly what
prompt_toolkit receives — the key sequence, character data, and aliases. Use this
to diagnose terminal quirks on the main REPL input path.
"""

from __future__ import annotations

import sys

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import KEY_ALIASES, Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl


def _aliases_for(data: str) -> list[str]:
    return sorted(name for name, value in KEY_ALIASES.items() if value == data)


def run_repl_key_inspector() -> None:
    """Run a minimal prompt_toolkit app that logs raw key events to stderr."""
    from jarn.tui.keyfix import apply_repl_keyfix

    apply_repl_keyfix()

    log = Buffer(read_only=True)
    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("c-q")
    def _quit(event) -> None:
        event.app.exit()

    @kb.add(Keys.Any)
    def _log(event) -> None:
        seq = event.key_sequence
        parts = [f"{kp.key!r}:{kp.data!r}" for kp in seq]
        data = event.data or ""
        aliases = _aliases_for(data) if data else []
        line = (
            f"sequence=[{', '.join(parts)}]  "
            f"data={data!r}  "
            f"aliases={aliases!r}\n"
        )
        sys.stderr.write(line)
        sys.stderr.flush()
        log.text += line

    header = FormattedTextControl(
        text=(
            "REPL key inspector — press any key (try Caps Lock / language switch).\n"
            "Each line: sequence, data, aliases. Ctrl+C or Ctrl+Q to quit.\n"
        )
    )
    app: Application[None] = Application(
        layout=Layout(
            HSplit([
                Window(header, height=2),
                Window(BufferControl(log), wrap_lines=True),
            ])
        ),
        key_bindings=kb,
        full_screen=False,
    )
    app.run()
