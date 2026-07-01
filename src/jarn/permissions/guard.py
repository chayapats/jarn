"""Hard danger-guard — the non-negotiable safety floor.

These checks run *before* permission modes and allowlists. A command/path the
guard flags as DANGEROUS always requires explicit confirmation, even in YOLO
mode; one flagged BLOCKED is refused outright and cannot be allowlisted.

This is deliberately conservative and pattern-based: it is a safety net, not a
substitute for sandboxing. Patterns target catastrophic, hard-to-undo actions.

.. note::

   The guard is a **net, not a sandbox**. It inspects the pre-shell command
   string with patterns; it does not parse shell syntax. Chaining via
   ``eval``/``bash -c``/``python -c``/heredocs/``$(...)``/base64-decoded
   payloads can hide a destructive command from these patterns. For untrusted
   code, run with ``execution.backend: docker`` or the OS sandbox instead of
   relying on this net. See ``SECURITY.md`` and ``docs/PERMISSIONS.md``.
"""

from __future__ import annotations

import re
import unicodedata
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
#: Matches the literal root (``/``), ``/*``, bare ``~``/``~/*``, and the home
#: env var in both ``$HOME`` and ``${HOME}`` spellings (brace form previously
#: escaped the root-target block, letting ``rm -rf ${HOME}`` through).
_ROOT_TARGET = re.compile(r"(?:^|\s)(?:/|/\*|~|~/\*?|\$HOME|\$\{HOME\})(?=\s|$)")

# `[^|;&]*` lets flags like `-C <path>` sit between the verb and its subcommand
# (`git -C /repo reset --hard`) without letting the match cross into a piped or
# chained command.
_GIT = r"\bgit\b[^|;&\n]*?"

#: Best-effort homoglyph defense. Maps common Latin-looking Cyrillic letters to
#: their ASCII lookalikes so a command like ``r\u043c -rf /`` (Cyrillic "em") is
#: collapsed to ``rm -rf /`` before pattern matching. NFKC normalization (below)
#: handles compatibility forms (fullwidth etc.) but does NOT cross scripts, so
#: this explicit table covers the confusables most likely to disguise a verb.
#: It is intentionally small and best-effort — a determined attacker can still
#: pick a code point we don't map; sandbox untrusted code instead.
_CONFUSABLES = str.maketrans({
    "\u0430": "a",   # Cyrillic small a
    "\u0435": "e",   # Cyrillic small ie
    "\u0436": "x",   # Cyrillic small zhe (looks like x in some faces) — skipped below
    "\u043e": "o",   # Cyrillic small o
    "\u0440": "p",   # Cyrillic small er
    "\u0441": "c",   # Cyrillic small es
    "\u0443": "y",   # Cyrillic small u
    "\u0445": "x",   # Cyrillic small ha
    "\u043c": "m",   # Cyrillic small em
    "\u0442": "t",   # Cyrillic small te
    "\u0410": "A",   # Cyrillic capital a
    "\u0412": "B",   # Cyrillic capital ve
    "\u0415": "E",   # Cyrillic capital ie
    "\u041a": "K",   # Cyrillic capital ka
    "\u041c": "M",   # Cyrillic capital em
    "\u041d": "H",   # Cyrillic capital en
    "\u041e": "O",   # Cyrillic capital o
    "\u0420": "P",   # Cyrillic capital er
    "\u0421": "C",   # Cyrillic capital es
    "\u0422": "T",   # Cyrillic capital te
    "\u0425": "X",   # Cyrillic capital ha
})
# Remove the ambiguous zhe mapping (kept out to avoid false positives).
_CONFUSABLES = str.maketrans(
    {k: v for k, v in _CONFUSABLES.items() if k != "\u0436"}
)


def _normalize(command: str) -> str:
    """NFKC-normalize + collapse homoglyphs + squeeze whitespace.

    NFKC first (compatibility/fullwidth forms), then the confusable table, then
    whitespace collapse so multi-space and tab-separated flags can't evade the
    single-space patterns below.
    """
    text = unicodedata.normalize("NFKC", command).translate(_CONFUSABLES)
    return " ".join(text.split())


# (compiled regex, level, human reason). Order matters: first match wins.
_RULES: list[tuple[re.Pattern[str], GuardLevel, str]] = [
    # Catastrophic, effectively irreversible — block outright.
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     GuardLevel.BLOCKED, "fork bomb"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), GuardLevel.BLOCKED, "filesystem format"),
    (re.compile(r"\bdd\b.*\bof=/dev/(sd|nvme|disk|rdisk)"),
     GuardLevel.BLOCKED, "raw write to a block device"),
    (re.compile(r">\s*/dev/(sd|nvme|disk)"), GuardLevel.BLOCKED, "redirect into a block device"),
    # A privileged container escapes isolation: block it outright.
    (re.compile(r"\bdocker\s+run\b[^|;&\n]*(?:--privileged|--pid=host|--net=host)\b"),
     GuardLevel.BLOCKED, "privileged container (isolation escape)"),

    # Dangerous but legitimate — require explicit confirmation.
    (re.compile(_GIT + r"\bpush\b[^|;&\n]*(--force\b|--force-with-lease\b|\s-f\b)"),
     GuardLevel.DANGEROUS, "force push"),
    (re.compile(_GIT + r"\breset\s+--hard\b"), GuardLevel.DANGEROUS, "hard reset (discards changes)"),
    (re.compile(_GIT + r"\bclean\s+-[a-zA-Z]*f"), GuardLevel.DANGEROUS, "git clean (deletes untracked)"),
    # Mass discard of working-tree changes: `git checkout .`, `git restore .`,
    # `git checkout -- *` / `git checkout -- .`. A single named file is not
    # matched — only the "everything" dot / wildcard forms.
    (re.compile(
        _GIT + r"\b(?:checkout|restore)\s+(?:\.|--\s+(?:\.|\*))(?:\s|$)"),
     GuardLevel.DANGEROUS, "mass discard of working-tree changes (checkout/restore .)"),
    # Recursive permission change — `-R` anywhere in the argv, so flag order
    # can't slip past (`chmod 777 -R .` previously escaped `\bchmod\s+-R\b`).
    (re.compile(r"\b(?:chmod|chown)\b[^|;&\n]*\s-R[a-zA-Z]*(?:\s|$)"),
     GuardLevel.DANGEROUS, "recursive permission change"),
    (re.compile(r"\bsudo\b"), GuardLevel.DANGEROUS, "elevated privileges"),
    (re.compile(r"\bcurl\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b"), GuardLevel.DANGEROUS, "pipe-to-shell install"),
    (re.compile(r"\bwget\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b"), GuardLevel.DANGEROUS, "pipe-to-shell install"),
    # Download-then-execute without a pipe: `curl -o f.sh url; sh f.sh`.
    (re.compile(r"\b(?:curl|wget)\b[^|;&\n]*\s-[oO]\b[^;\n]*;\s*(?:sh|bash|zsh)\b"),
     GuardLevel.DANGEROUS, "download-then-execute"),
    # Decode-and-execute: `base64 -d … | sh` hides the payload from the net.
    (re.compile(r"\bbase64\b[^|;&\n]*\s-d\b[^|;&\n]*\|\s*(?:sh|bash|zsh)\b"),
     GuardLevel.DANGEROUS, "base64-decoded payload piped to shell"),
    (re.compile(r"\bkill(all)?\s+-9\b"), GuardLevel.DANGEROUS, "force kill"),
    (re.compile(r"\bfind\b[^|;&\n]*\s-delete\b"), GuardLevel.DANGEROUS, "find -delete"),
    # `find -exec rm` / `-execdir rm` deletes matched files (mass removal).
    (re.compile(r"\bfind\b[^|;&\n]*\s-exec(?:dir)?\s+rm\b"),
     GuardLevel.DANGEROUS, "find -exec rm (mass delete)"),
    # Package managers / remote-code runners: postinstall scripts and `npx`/`bunx`
    # fetch+run arbitrary code. DANGEROUS (not BLOCKED) so trusted workflows still
    # work with one confirmation.
    (re.compile(r"\b(?:npm\s+install|pnpm\s+install|yarn\s+add)\b"),
     GuardLevel.DANGEROUS, "package install (postinstall scripts run code)"),
    (re.compile(r"\b(?:pip\s+install|uv\s+pip\s+install|uv\s+add|npx|bunx)\b"),
     GuardLevel.DANGEROUS, "package manager / remote-code runner"),
    # Power control — a CI/agent context almost never legitimately halts the host.
    (re.compile(r"\b(?:shutdown|reboot|halt)\b"), GuardLevel.DANGEROUS, "host power control"),
    # Truncate-to-zero wipes a file's contents (often a config/log).
    (re.compile(r"\btruncate\b[^|;&\n]*\s-s\s*0\b"), GuardLevel.DANGEROUS, "truncate file to zero bytes"),
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
    normalized = _normalize(command)
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
