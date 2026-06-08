"""Hard danger-guard — the non-negotiable safety floor.

These checks run *before* permission modes and allowlists. A command/path the
guard flags as DANGEROUS always requires explicit confirmation, even in YOLO
mode; one flagged BLOCKED is refused outright and cannot be allowlisted.

This is deliberately conservative and pattern-based: it is a safety net, not a
substitute for sandboxing. Patterns target catastrophic, hard-to-undo actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class GuardLevel(str, Enum):
    SAFE = "safe"            # guard has no opinion; defer to engine
    DANGEROUS = "dangerous"  # must confirm explicitly, even in YOLO
    BLOCKED = "blocked"      # refused outright, cannot be allowlisted


@dataclass(slots=True, frozen=True)
class GuardVerdict:
    level: GuardLevel
    reason: str = ""

    @property
    def is_safe(self) -> bool:
        return self.level is GuardLevel.SAFE


# `rm` is handled by flag-presence (below) rather than one positional regex, so
# split (`-r -f`) and long (`--recursive --force`) forms can't slip past.
_RM = re.compile(r"\brm\b")
_RM_RECURSIVE = re.compile(r"(?:^|\s)(?:--recursive|-[a-zA-Z]*r[a-zA-Z]*)(?=\s|$)")
_RM_FORCE = re.compile(r"(?:^|\s)(?:--force|-[a-zA-Z]*f[a-zA-Z]*)(?=\s|$)")
#: A root/home target appearing as its own token (the catastrophic case).
_ROOT_TARGET = re.compile(r"(?:^|\s)(?:/|/\*|~|~/\*?|\$HOME)(?=\s|$)")

# `[^|;&]*` lets flags like `-C <path>` sit between the verb and its subcommand
# (`git -C /repo reset --hard`) without letting the match cross into a piped or
# chained command.
_GIT = r"\bgit\b[^|;&\n]*?"

# (compiled regex, level, human reason). Order matters: first match wins.
_RULES: list[tuple[re.Pattern[str], GuardLevel, str]] = [
    # Catastrophic, effectively irreversible — block outright.
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     GuardLevel.BLOCKED, "fork bomb"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), GuardLevel.BLOCKED, "filesystem format"),
    (re.compile(r"\bdd\b.*\bof=/dev/(sd|nvme|disk|rdisk)"),
     GuardLevel.BLOCKED, "raw write to a block device"),
    (re.compile(r">\s*/dev/(sd|nvme|disk)"), GuardLevel.BLOCKED, "redirect into a block device"),

    # Dangerous but legitimate — require explicit confirmation.
    (re.compile(_GIT + r"\bpush\b[^|;&\n]*(--force\b|--force-with-lease\b|\s-f\b)"),
     GuardLevel.DANGEROUS, "force push"),
    (re.compile(_GIT + r"\breset\s+--hard\b"), GuardLevel.DANGEROUS, "hard reset (discards changes)"),
    (re.compile(_GIT + r"\bclean\s+-[a-zA-Z]*f"), GuardLevel.DANGEROUS, "git clean (deletes untracked)"),
    (re.compile(r"\bchmod\s+-R\b|\bchown\s+-R\b"), GuardLevel.DANGEROUS, "recursive permission change"),
    (re.compile(r"\bsudo\b"), GuardLevel.DANGEROUS, "elevated privileges"),
    (re.compile(r"\bcurl\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b"), GuardLevel.DANGEROUS, "pipe-to-shell install"),
    (re.compile(r"\bwget\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b"), GuardLevel.DANGEROUS, "pipe-to-shell install"),
    (re.compile(r"\bkill(all)?\s+-9\b"), GuardLevel.DANGEROUS, "force kill"),
    (re.compile(r"\bfind\b[^|;&\n]*\s-delete\b"), GuardLevel.DANGEROUS, "find -delete"),
]


def _rm_verdict(normalized: str) -> GuardVerdict | None:
    """Classify an ``rm`` invocation by *flag presence* (order/spelling agnostic).

    Recursive + force together is destructive; aimed at a root/home token it is
    catastrophic. Returns ``None`` when ``rm`` isn't a recursive-force delete."""
    if not _RM.search(normalized) or not _RM_RECURSIVE.search(normalized):
        return None
    # Recursive delete aimed at a root/home token is catastrophic — block it even
    # without -f (force only suppresses prompts; the deletion still happens).
    if _ROOT_TARGET.search(normalized):
        return GuardVerdict(GuardLevel.BLOCKED, "recursive delete of a root/home path")
    if _RM_FORCE.search(normalized):
        return GuardVerdict(GuardLevel.DANGEROUS, "recursive force-delete")
    return None


def inspect_command(command: str) -> GuardVerdict:
    """Classify a shell command against the danger-guard rules."""
    normalized = " ".join(command.split())
    rm = _rm_verdict(normalized)
    if rm is not None:
        return rm
    for pattern, level, reason in _RULES:
        if pattern.search(normalized):
            return GuardVerdict(level, reason)
    return GuardVerdict(GuardLevel.SAFE)


def inspect_path_write(path: str, *, in_scope: bool) -> GuardVerdict:
    """Classify a filesystem write. Out-of-scope writes are dangerous."""
    if not in_scope:
        return GuardVerdict(GuardLevel.DANGEROUS, f"write outside the project scope: {path}")
    # Writing over VCS internals or shell rc files is suspicious.
    risky_suffixes = (".git/", "/.ssh/", "/.aws/credentials")
    norm = path.replace("\\", "/")
    if any(seg in norm for seg in risky_suffixes):
        return GuardVerdict(GuardLevel.DANGEROUS, f"write to a sensitive location: {path}")
    return GuardVerdict(GuardLevel.SAFE)
