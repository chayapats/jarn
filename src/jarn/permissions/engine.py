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
    #: Extra path-like candidates a READ must ALSO be judged against, beyond
    #: ``target``. A ``grep``/``glob`` carries both a search ``path`` and a
    #: ``glob`` that can itself narrow the search to a secret (``glob='**/.env'``),
    #: so a benign ``path`` must not be able to mask a sensitive ``glob``. Every
    #: candidate is tested against the sensitive-read globs AND the read-deny
    #: rules (see :meth:`PermissionEngine._is_sensitive_read` / :meth:`_matches`).
    #: Empty for non-read actions.
    read_targets: tuple[str, ...] = ()


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

    # -- read-result filtering (used by jarn.agent.read_filter) --------------
    #
    # The pre-exec gate sees only a read's SCOPE (its ``path``/``glob``), so a
    # broad content-returning read — ``grep(pattern='TOKEN=', path='/repo')`` —
    # is auto-ALLOWed on the benign scope yet still returns the CONTENTS of every
    # matching file, including ``.env``/SSH keys. The result-filter middleware
    # closes that by re-checking each matched file's path through the methods
    # below, so the engine stays the single source of truth for what a read may
    # surface.

    def is_read_denied_path(self, path: str) -> bool:
        """True when a filesystem *path* matches an explicit read *deny* rule
        (config ``rules.deny`` or a session deny).

        Defense-in-depth backstop for ``read_file``: a denied read is already
        blocked pre-exec, but the result-filter re-checks so a denied file's
        contents can never reach the model even if that gate is bypassed."""
        return self._matches(Action(ActionKind.READ, target=path), self._all_deny())

    def read_content_blocked(self, path: str) -> bool:
        """True when a file at *path* must not have its CONTENTS surfaced by a
        broad read tool (``grep``): it matches a read *deny* rule OR a
        sensitive-read glob, and is NOT covered by an explicit *allow* rule.

        Mirrors :meth:`evaluate`'s precedence (deny > allow > sensitive-read) so
        the result-filter and the pre-exec gate agree: a broad ``grep`` over a
        benign scope silently drops hits from ``.env``/keys (the exfiltration the
        gate cannot catch), while an explicitly allow-listed secret path still
        comes through."""
        act = Action(ActionKind.READ, target=path)
        if self._matches(act, self._all_deny()):
            return True
        if self._matches(act, self._all_allow()):
            return False
        return self.is_sensitive_read_path(path)

    # -- internals ----------------------------------------------------------

    def _guard_for(self, action: Action):
        if action.kind is ActionKind.SHELL:
            # Thread the per-host network egress policy so the guard can flag
            # curl/wget to denied / non-allowlisted hosts (best-effort).
            return inspect_command(action.target, self.rules.network)
        if action.kind is ActionKind.WRITE:
            return inspect_path_write(action.target, in_scope=self._in_scope(action.target))
        from jarn.permissions.guard import GuardVerdict
        return GuardVerdict(GuardLevel.SAFE)

    def _mode_decision(self, action: Action) -> PermissionResult:
        mode = self.mode
        if action.kind is ActionKind.READ:
            # Reads reaching here already cleared the deny check (line ~121) and
            # the allow check (line ~132), so an explicit deny/allow rule wins.
            # Otherwise reads auto-ALLOW EXCEPT for sensitive secret stores: those
            # are confirmed (ASK) in every mode — including YOLO — so the agent
            # cannot silently read ``.env``/``id_rsa``/``.aws/credentials`` and
            # exfiltrate them through an allowed network tool. An explicit allow
            # rule (checked earlier) is the escape hatch for a specific path.
            if self._is_sensitive_read(action):
                return PermissionResult(
                    Decision.ASK, "sensitive-path read requires confirmation"
                )
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

    def _is_sensitive_read(self, action: Action) -> bool:
        """True when ANY of a READ's candidate targets matches a sensitive glob.

        A ``grep``/``glob`` is judged against its search ``path`` AND its ``glob``
        (see :attr:`Action.read_targets`), so ``grep(path='/repo', glob='**/.env')``
        is caught even though ``/repo`` alone is benign.
        """
        return any(
            self.is_sensitive_read_path(cand)
            for cand in self._read_candidates(action)
        )

    @staticmethod
    def _read_candidates(action: Action) -> tuple[str, ...]:
        """Every path-like target a READ is judged against: the primary ``target``
        plus any extra ``read_targets`` (a grep/glob ``glob`` value), de-duplicated
        with empties dropped (order preserved for stable reasoning)."""
        out: list[str] = []
        for cand in (action.target, *action.read_targets):
            if cand and cand not in out:
                out.append(cand)
        return tuple(out)

    def is_sensitive_read_path(self, path: str) -> bool:
        """True when a filesystem *path* matches a configured sensitive-read glob.

        The SINGLE source of truth shared by the READ mode-decision here and the
        result-filter middleware (:mod:`jarn.agent.read_filter`).

        Matching runs against the raw normalized path AND a leading-slash form,
        so a ``**/``-anchored pattern also catches a bare relative target
        (``.env`` vs ``**/.env``) WITHOUT the false positives a basename-only
        match would create (a plain ``config`` must not match ``**/.git/config``).
        ``fnmatch``'s ``*`` spans ``/``, so ``*.pem`` matches a .pem file at any
        depth. An empty ``sensitive_read_globs`` disables the check entirely.
        """
        globs = self.rules.sensitive_read_globs
        if not globs or not path:
            return False
        norm = path.replace("\\", "/")
        candidates = {norm, norm if norm.startswith("/") else "/" + norm}
        return any(
            fnmatch.fnmatch(cand, pattern)
            for pattern in globs
            for cand in candidates
        )

    def _matches(self, action: Action, patterns: list[str]) -> bool:
        candidates = {action.target, self._rule_for(action)}
        # A READ may carry extra path-like candidates (grep/glob ``glob`` value);
        # a deny/allow rule matching ANY of them applies, so a benign scope can't
        # mask a sensitive glob from an explicit deny.
        if action.kind is ActionKind.READ:
            candidates.update(cand for cand in action.read_targets if cand)
        for pattern in patterns:
            for cand in candidates:
                if cand == pattern or fnmatch.fnmatch(cand, pattern):
                    return True
        return False

    def _all_allow(self) -> list[str]:
        return [*self.rules.allow, *self._session_allow]

    def _all_deny(self) -> list[str]:
        return [*self.rules.deny, *self._session_deny]
