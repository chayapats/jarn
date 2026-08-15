"""``/diff [staged|all|session]`` — colored unified diff from git / recap files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarn.agent.git_commands import diff_for_paths, gather_diff
from jarn.commands.help import usage_error
from jarn.controller.commands.diagnostics import _transcript_recap
from jarn.controller.core import CommandResult

if TYPE_CHECKING:
    from jarn.controller.core import Controller

_DIFF_MODES = frozenset({"", "staged", "all", "session"})


def cmd_diff(ctrl: Controller, args: str) -> CommandResult:
    sub = args.strip().lower()
    if sub not in _DIFF_MODES:
        return CommandResult(usage_error("diff"))
    root = ctrl.project_root
    if root is None:
        return CommandResult("No project root.")
    snap = gather_diff(root)
    if not snap.is_repo:
        return CommandResult("Not a git repository.")
    if sub == "session":
        recap = _transcript_recap(ctrl.sessions.transcript_path(ctrl.thread_id))
        files = [str(path) for path in recap.get("files") or []]
        if not files:
            return CommandResult("No files this session has edited.")
        body = diff_for_paths(root, files).strip()
        if not body:
            return CommandResult("No git changes in session files.")
        return CommandResult(body)
    use_staged = sub == "staged" or (sub == "" and snap.has_staged)
    if use_staged:
        body = snap.staged.strip()
        if not body:
            return CommandResult("No staged changes.")
        return CommandResult(body)
    parts = [p.strip() for p in (snap.unstaged, snap.untracked) if p and p.strip()]
    body = "\n".join(parts).strip()
    if not body:
        return CommandResult("No working-tree changes.")
    return CommandResult(body)
