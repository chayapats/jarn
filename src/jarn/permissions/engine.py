"""The permission engine — combines coarse modes, fine-grained rules, the
danger-guard, and remembered approvals into a single decision per action.

Decision precedence (highest first):
  1. danger-guard BLOCKED        -> DENY (un-allowlistable)
  2. explicit deny rule          -> DENY
  3. danger-guard DANGEROUS      -> ASK (force confirm, even in YOLO)
  4. remembered/allowlisted      -> ALLOW
  5. coarse permission mode       -> ALLOW | ASK | DENY
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from jarn.config.schema import PermissionMode, PermissionRules
from jarn.config.yaml_store import ConfigCorruptError
from jarn.permissions.guard import GuardLevel, inspect_command, inspect_path_write

_log = logging.getLogger("jarn")

#: Programs whose payload is an *argument*, so "program + first arg" would
#: allowlist arbitrary code (e.g. ``bash -c <anything>``). Remembered approvals
#: for these must match the full command, never a generalized prefix.
#: Network tools that only read remote state (no mutations). Auto-edit auto-allows
#: these; MCP tools and mutating async-subagent calls stay ASK.
_READONLY_NETWORK_TOOLS = frozenset({
    "web_search",
    "web_fetch",
    "check_async_task",
    "list_async_tasks",
})

_WRAPPER_PROGRAMS = frozenset({
    "bash", "sh", "zsh", "dash", "fish", "ksh",
    "python", "python2", "python3", "node", "ruby", "perl", "php",
    "env", "xargs", "nohup", "timeout", "watch", "eval", "exec",
})


class ActionKind(str, Enum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"
    NETWORK = "network"


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RememberScope(str, Enum):
    ONCE = "once"        # this single action only
    SESSION = "session"  # remembered until the app exits
    ALWAYS = "always"    # persisted to project config allowlist


@dataclass(slots=True)
class Action:
    kind: ActionKind
    #: The shell command (SHELL), file path (READ/WRITE) or URL/host (NETWORK).
    target: str
    #: Originating tool name, for hook matching and logging.
    tool: str | None = None


@dataclass(slots=True, frozen=True)
class PermissionResult:
    decision: Decision
    reason: str
    dangerous: bool = False
    #: When True, an ALWAYS approval is refused (guard-dangerous actions).
    block_remember_always: bool = False


@dataclass(slots=True)
class PermissionEngine:
    """Evaluates actions for the lifetime of one session.

    ``mode`` and the persisted ``rules`` come from config; session approvals are
    accumulated in-memory. ``project_root`` is the PRIMARY root (it anchors
    relative write targets and backs project context / checkpoints); ``roots``
    holds any ADDED roots (from ``--add-dir`` / ``/add-dir``). A write is
    in-scope when it resolves under the primary OR any added root — see
    :meth:`_in_scope`. ``project_root=None`` with no added roots means "no scope
    restriction" (e.g. running outside a project).
    """

    mode: PermissionMode = PermissionMode.ASK
    rules: PermissionRules = field(default_factory=PermissionRules)
    project_root: Path | None = None
    #: Additional in-scope roots beyond ``project_root`` (added mid-session by
    #: ``/add-dir`` or at launch by ``--add-dir``). Each is enforced with the same
    #: per-root ``resolve()`` symlink discipline as the primary. Context loading
    #: and checkpoint/undo stay PRIMARY-ONLY — these widen the WRITE scope only.
    roots: tuple[Path, ...] = ()
    #: Optional sink for ALWAYS-scoped rules so they persist across processes
    #: (wired by the controller to a :class:`PermissionRuleStore`).
    persist: Callable[[str], object] | None = None
    _session_allow: list[str] = field(default_factory=list)
    _session_deny: list[str] = field(default_factory=list)

    # -- public API ---------------------------------------------------------

    def evaluate(self, action: Action) -> PermissionResult:
        guard = self._guard_for(action)

        if guard.level is GuardLevel.BLOCKED:
            return PermissionResult(
                Decision.DENY, f"blocked by danger-guard: {guard.reason}",
                dangerous=True, block_remember_always=True,
            )

        if self._matches(action, self._all_deny()):
            return PermissionResult(Decision.DENY, "matched a deny rule")

        if guard.level is GuardLevel.DANGEROUS:
            # Force confirmation regardless of mode/allowlist; never auto-allow,
            # never persist an ALWAYS rule for it.
            return PermissionResult(
                Decision.ASK, f"danger-guard: {guard.reason}",
                dangerous=True, block_remember_always=True,
            )

        if self._matches(action, self._all_allow()):
            return PermissionResult(Decision.ALLOW, "matched an allow rule")

        return self._mode_decision(action)

    def remember(self, action: Action, scope: RememberScope) -> str | None:
        """Record an approval. For ALWAYS, also persist the rule via
        :attr:`persist` (if wired) and return it; otherwise return ``None``.

        SESSION and ALWAYS both take effect immediately (in-memory allowlist);
        ALWAYS additionally survives across processes through ``persist``.
        """
        rule = self._rule_for(action)
        if scope is RememberScope.ONCE:
            return None
        if rule not in self._session_allow:
            self._session_allow.append(rule)
        if scope is RememberScope.ALWAYS:
            if self.persist is not None:
                try:
                    self.persist(rule)
                except ConfigCorruptError as exc:
                    # The project config is corrupt; the in-memory allow still
                    # applies for this session. Persistence is skipped — the
                    # user sees the repair hint at the next config load.
                    _log.warning("Could not persist allow-rule: %s", exc)
            return rule
        return None

    def deny_session(self, action: Action) -> None:
        rule = self._rule_for(action)
        if rule not in self._session_deny:
            self._session_deny.append(rule)

    # -- internals ----------------------------------------------------------

    def _guard_for(self, action: Action):
        if action.kind is ActionKind.SHELL:
            return inspect_command(action.target)
        if action.kind is ActionKind.WRITE:
            return inspect_path_write(action.target, in_scope=self._in_scope(action.target))
        from jarn.permissions.guard import GuardVerdict
        return GuardVerdict(GuardLevel.SAFE)

    def _mode_decision(self, action: Action) -> PermissionResult:
        mode = self.mode
        if action.kind is ActionKind.READ:
            return PermissionResult(Decision.ALLOW, "reads are always permitted")

        if mode is PermissionMode.PLAN:
            return PermissionResult(Decision.DENY, "plan mode is read-only")

        if mode is PermissionMode.YOLO:
            return PermissionResult(Decision.ALLOW, "yolo mode")

        if mode is PermissionMode.AUTO_EDIT:
            if action.kind is ActionKind.WRITE:
                if self._in_scope(action.target):
                    return PermissionResult(Decision.ALLOW, "auto-edit: in-scope write")
                return PermissionResult(Decision.ASK, "auto-edit: write is out of scope")
            if (
                action.kind is ActionKind.NETWORK
                and action.tool in _READONLY_NETWORK_TOOLS
            ):
                return PermissionResult(Decision.ALLOW, "auto-edit: read-only network")

        # ASK (and AUTO_EDIT for shell / other network) -> confirm.
        return PermissionResult(Decision.ASK, f"{mode.value} mode requires confirmation")

    def _scope_roots(self) -> list[Path]:
        """The active in-scope roots, PRIMARY FIRST.

        ``project_root`` (the primary) leads, followed by any added ``roots``.
        Empty when neither is set (→ no scope restriction).
        """
        roots: list[Path] = []
        if self.project_root is not None:
            roots.append(self.project_root)
        roots.extend(self.roots)
        return roots

    def _in_scope(self, target: str) -> bool:
        roots = self._scope_roots()
        if not roots:
            return True
        # Resolve relative targets against the PRIMARY root, NOT the process CWD:
        # an agent in a subdir writing "../outside" must be judged by intent
        # relative to the project it works in, not by where the shell happens to
        # be running. ``primary / target`` keeps absolute targets as-is and
        # anchors relative ones (including ``~`` via expanduser).
        #
        # ``resolve()`` follows symlinks, so a symlink inside ANY root that
        # points outside every root resolves out-of-scope and is rejected for
        # writes — the same discipline holds per-root for added roots as for the
        # primary. This is an *intent* check; the tool layer (backend FS guard +
        # OS/Docker sandbox) enforces the same bound again at syscall time
        # (TOCTOU mitigation), using the SAME roots set.
        try:
            primary = roots[0].resolve()
            resolved = (primary / target).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        for root in roots:
            try:
                r = root.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved == r or r in resolved.parents:
                return True
        return False

    def _rule_for(self, action: Action) -> str:
        if action.kind is ActionKind.SHELL:
            parts = action.target.split()
            if len(parts) < 2:
                return action.target
            prog = parts[0].rsplit("/", 1)[-1]  # strip any path
            first_arg = parts[1]
            # Don't generalize wrapper/eval invocations: "bash -c <script>" or a
            # flag-led command would allowlist arbitrary payloads under one rule.
            # Match the exact command instead.
            if prog in _WRAPPER_PROGRAMS or first_arg.startswith("-"):
                return action.target
            # ``npm run <script>`` must remember the exact script — generalizing to
            # ``npm run`` would allowlist every package script after one approval.
            if prog == "npm" and len(parts) >= 3 and parts[1] == "run":
                return action.target
            # Otherwise generalize to program + subcommand so "npm test" reruns.
            return f"{parts[0]} {first_arg}"
        return action.target

    def _matches(self, action: Action, patterns: list[str]) -> bool:
        candidates = {action.target, self._rule_for(action)}
        for pattern in patterns:
            for cand in candidates:
                if cand == pattern or fnmatch.fnmatch(cand, pattern):
                    return True
        return False

    def _all_allow(self) -> list[str]:
        return [*self.rules.allow, *self._session_allow]

    def _all_deny(self) -> list[str]:
        return [*self.rules.deny, *self._session_deny]
