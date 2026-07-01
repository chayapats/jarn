"""``/commit`` and ``/review``: gather the working-tree diff and build the
seeded agent prompt.

The prompt *builders* are pure functions of a :class:`GitDiff` so they are
trivially unit-testable; :func:`gather_diff` is the thin git-running layer the
REPL calls. The REPL embeds the diff directly in the seeded turn so the agent
doesn't spend a tool round-trip re-reading it (and so ``/review`` works even
when shell is gated by the permission mode).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Cap the diff text embedded into a seeded prompt so a huge change set doesn't
#: blow the context window. The agent can always read more with its own tools.
_MAX_DIFF_CHARS = 24_000


@dataclass(frozen=True, slots=True)
class GitDiff:
    """Snapshot of the working tree relevant to commit/review."""

    is_repo: bool
    staged: str = ""
    unstaged: str = ""
    untracked: str = ""
    status: str = ""
    stderr: str = ""

    @property
    def has_staged(self) -> bool:
        return bool(self.staged.strip())

    @property
    def has_changes(self) -> bool:
        return bool(
            self.staged.strip() or self.unstaged.strip() or self.untracked.strip()
        )


def _run_git(root: Path, *args: str) -> tuple[int, str, str]:
    """Run a git command under ``root``; return (returncode, stdout, stderr).

    Never raises — a missing git binary or a non-repo directory yields a
    non-zero code with empty output so callers degrade gracefully.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""
    return proc.returncode, proc.stdout, proc.stderr


def _untracked_diff(root: Path) -> tuple[str, str]:
    """Build a unified diff for untracked files (``/dev/null`` → file)."""
    code, listing, err = _run_git(
        root, "ls-files", "--others", "--exclude-standard"
    )
    if code != 0:
        return "", err.strip()
    parts: list[str] = []
    stderr_bits: list[str] = []
    for rel in listing.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        dcode, diff, derr = _run_git(root, "diff", "--no-index", "/dev/null", rel)
        if diff.strip():
            parts.append(diff)
        if dcode != 0 and derr.strip():
            stderr_bits.append(derr.strip())
    return "\n".join(parts), "\n".join(stderr_bits) or err.strip()


def gather_diff(root: Path) -> GitDiff:
    """Collect staged + unstaged diffs, untracked new files, and short status."""
    code, _, _ = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return GitDiff(
            is_repo=False, staged="", unstaged="", untracked="", status=""
        )
    _, staged, staged_err = _run_git(root, "diff", "--staged")
    _, unstaged, unstaged_err = _run_git(root, "diff")
    _, status, status_err = _run_git(root, "status", "--short")
    untracked, untracked_err = _untracked_diff(root)
    stderr = "\n".join(
        s for s in (staged_err, unstaged_err, status_err, untracked_err) if s.strip()
    ).strip()
    return GitDiff(
        is_repo=True,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        status=status,
        stderr=stderr,
    )


def _clip(text: str) -> str:
    if len(text) <= _MAX_DIFF_CHARS:
        return text
    return text[:_MAX_DIFF_CHARS] + "\n… (diff truncated)"


def commit_prompt(diff: GitDiff) -> str | None:
    """Seeded prompt for ``/commit``; ``None`` when there's nothing to commit."""
    if not diff.is_repo or not diff.has_changes:
        return None
    if diff.has_staged:
        body = diff.staged
        staging_note = "There are staged changes."
    else:
        body = "\n".join(
            part for part in (diff.unstaged, diff.untracked) if part.strip()
        )
        staging_note = (
            "Nothing is staged yet — stage the relevant changes first with "
            "`git add`."
        )
    stderr_note = ""
    if diff.stderr:
        stderr_note = f"\nGit stderr:\n```\n{diff.stderr[:2000]}\n```\n"
    return (
        "Create a git commit for the current changes.\n\n"
        f"{staging_note} Write a concise commit message that follows this "
        "repository's existing convention (check recent `git log` if unsure), "
        "show it to me, then run `git commit`. Do not push.\n\n"
        f"Working-tree status:\n```\n{diff.status.strip()}\n```\n\n"
        f"Diff:\n```diff\n{_clip(body).strip()}\n```\n"
        f"{stderr_note}"
    )


def review_prompt(diff: GitDiff) -> str | None:
    """Seeded prompt for ``/review``; ``None`` when there's nothing to review."""
    if not diff.is_repo:
        return None
    body = "\n".join(
        part
        for part in (diff.staged, diff.unstaged, diff.untracked)
        if part.strip()
    )
    if not body.strip():
        return None
    stderr_note = ""
    if diff.stderr:
        stderr_note = f"\nGit stderr:\n```\n{diff.stderr[:2000]}\n```\n"
    return (
        "Review the following diff of the current working tree. This is a "
        "read-only review — do NOT edit any files. Report correctness bugs "
        "first (cite file:line), then quality / simplification notes. Be "
        "concise and only flag real issues.\n\n"
        f"Diff:\n```diff\n{_clip(body).strip()}\n```\n"
        f"{stderr_note}"
    )
