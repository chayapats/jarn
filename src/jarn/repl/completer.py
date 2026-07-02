"""REPL input completion and shell-escape lexer."""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.lexers import Lexer

from jarn.tui.completion import CompletionProvider


class _ShellEscapeLexer(Lexer):
    """Colour the input red while it is a ``!`` shell escape.

    A ``!``-prefixed line runs directly on the host shell — no agent, no
    permission engine, no danger-guard — so the live input is rendered in the
    ``shell-escape`` style (red + bold) to make that unmistakable as the user
    types, distinct from a normal agent prompt.
    """

    def lex_document(self, document):  # noqa: ANN001 - prompt_toolkit Document
        is_shell = document.text.lstrip().startswith("!")

        def get_line(lineno: int):
            text = document.lines[lineno]
            return [("class:shell-escape" if is_shell else "", text)]

        return get_line


class _SlashFileCompleter(Completer):
    """Bridges :class:`CompletionProvider` to prompt_toolkit completions."""

    def __init__(self, provider_factory: Callable[[], CompletionProvider]) -> None:
        self._factory = provider_factory

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if "\n" in text:
            return
        for cand in self._factory().complete(text):
            yield Completion(
                cand.replacement,
                start_position=-len(text),
                display=cand.label,
                display_meta=cand.description or None,
            )
