"""Input completion logic for the prompt — ``/command`` and ``@file`` candidates.

Kept UI-free and deterministic so it can be unit-tested; the Textual layer just
renders the returned candidates and applies the chosen replacement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class Completion:
    """A single candidate. ``replacement`` is the full new input text if chosen."""

    label: str
    replacement: str
    kind: str  # "command" | "file"
    description: str = ""


@dataclass(slots=True)
class CompletionProvider:
    """Slash-command and ``@file`` completion for the REPL prompt."""

    command_catalog: dict[str, str]  # name → short description
    project_root: Path | None = None
    max_files: int = 12

    @property
    def commands(self) -> list[str]:
        """Command names (for tests and legacy callers)."""
        return sorted(self.command_catalog)

    def complete(self, text: str) -> list[Completion]:
        """Return candidates for the current single-line input ``text``."""
        # Command completion: the whole line is "/prefix" with no space yet.
        if text.startswith("/") and " " not in text:
            return self._commands(text[1:])

        # File completion: the last whitespace-token starts with "@".
        token = text.rsplit(" ", 1)[-1] if " " in text else text
        if token.startswith("@"):
            prefix = text[: len(text) - len(token)]
            return self._files(prefix, token[1:])
        return []

    def _commands(self, prefix: str) -> list[Completion]:
        prefix = prefix.lower()
        out: list[Completion] = []
        for name in sorted(self.command_catalog):
            if name.lower().startswith(prefix):
                out.append(
                    Completion(
                        f"/{name}",
                        f"/{name} ",
                        "command",
                        description=self.command_catalog.get(name, ""),
                    )
                )
        return out

    def _files(self, prefix_text: str, frag: str) -> list[Completion]:
        root = self.project_root or Path.cwd()
        if not root.is_dir():
            return []
        # Split the fragment into a directory part and a name part.
        frag_path = Path(frag)
        search_dir = root / frag_path.parent if frag_path.parent != Path(".") else root
        name_prefix = frag_path.name
        if not search_dir.is_dir():
            return []

        out: list[Completion] = []
        try:
            entries = sorted(search_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError:
            return []
        for entry in entries:
            if entry.name.startswith(".") and not name_prefix.startswith("."):
                continue
            if not entry.name.lower().startswith(name_prefix.lower()):
                continue
            rel = entry.relative_to(root)
            suffix = "/" if entry.is_dir() else ""
            replacement = f"{prefix_text}@{rel}{suffix}"
            out.append(Completion(f"@{rel}{suffix}", replacement, "file"))
            if len(out) >= self.max_files:
                break
        return out
