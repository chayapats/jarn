"""The permission engine — combines coarse modes, fine-grained rules, the
danger-guard, and remembered approvals into a single decision per action.

Decision precedence (highest first):
  1. danger-guard BLOCKED        -> DENY (un-allowlistable)
  2. jarn's own secret store     -> DENY (un-allowlistable)
  3. explicit deny rule          -> DENY
  4. danger-guard DANGEROUS      -> ASK (force confirm, even in YOLO)
  5. remembered/allowlisted      -> ALLOW
  6. coarse permission mode       -> ALLOW | ASK | DENY
"""

from __future__ import annotations

import fnmatch
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath

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

#: fnmatch glob metacharacters. A READ target/pattern containing any of these is a
#: PATTERN, not a concrete file: it is matched textually (metacharacters preserved),
#: never resolved as a filesystem path.
_GLOB_METACHARS = frozenset("*?[")

# Generated approvals use a typed envelope inside the existing string-list schema.
# Hand-authored legacy glob rules remain supported, while a remembered approval can
# no longer widen across action kind, originating tool, or another workspace.
_SCOPED_RULE_PREFIX = "jarn-scope:v1:"

#: LEXICAL backstop for jarn's own secret store. Identity matching against
#: :func:`~jarn.config.paths.secret_store_dirs` is the primary check; these
#: patterns catch the two cases identity cannot: a GLOB candidate
#: (``grep(glob='.jarn/secrets/**')`` narrows a benign scope onto the store but has
#: no file to resolve), and a host where ``$HOME`` is unanswerable so no store
#: directory could be located at all. They also cover a *project*-level
#: ``.jarn/secrets/`` — same shape, same treatment. Matched against CASEFOLDED
#: aliases (so keep them lowercase), for the reason in :func:`_path_within`.
#:
#: Deliberately NOT part of ``sensitive_read_globs``: that list routes to ASK and is
#: opt-out (``sensitive_read_globs: []``). The secret store is a hard floor — see
#: :meth:`PermissionEngine._touches_secret_store`.
_SECRET_STORE_GLOBS: tuple[str, ...] = (
    "**/.jarn/secrets",
    "**/.jarn/secrets/**",
)


def _is_glob(text: str) -> bool:
    """True when *text* carries fnmatch glob metacharacters (so it is a pattern,
    not a concrete path to resolve to a file identity)."""
    return any(ch in _GLOB_METACHARS for ch in text)


def _path_within(candidate: Path, directory: Path) -> bool:
    """True when *candidate* IS *directory* or sits beneath it — matched exactly
    AND case-insensitively.

    ``Path.resolve()`` follows symlinks but does NOT canonicalize case, and both
    ``PurePath.__eq__`` and ``fnmatch`` are byte-exact on POSIX. On a
    case-insensitive filesystem — APFS (the macOS default) and NTFS —
    ``~/.jarn/SECRETS/jarn/groq`` therefore opens the very same inode as the store
    file while comparing unequal to ``~/.jarn/secrets``, which is enough to walk
    straight through a path-identity check. ``str.casefold`` closes that, and also
    folds the Unicode aliases such a filesystem accepts: APFS opens
    ``.jarn/ſecretſ`` as ``.jarn/secrets``, and U+017F casefolds to ``s`` (which a
    plain ``lower()`` would miss).

    On a genuinely case-sensitive filesystem this can match a DIFFERENT directory
    that merely differs in case. For a hard floor that is the safe direction, and
    no legitimate read is lost by it.
    """
    if candidate == directory or directory in candidate.parents:
        return True
    parts = directory.parts
    if len(candidate.parts) < len(parts):
        return False
    return all(
        a.casefold() == b.casefold()
        for a, b in zip(candidate.parts, parts, strict=False)  # candidate may be deeper
    )


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


#: The one verdict for any action touching jarn's own secret store. Un-allowlistable
#: by construction: it is returned ABOVE the allow tier in :meth:`evaluate`, so no
#: config ``allow``, session approval or permission mode (``yolo`` included) reaches
#: it, and ``block_remember_always`` keeps it out of the persisted rule store.
_SECRET_STORE_DENIAL = PermissionResult(
    Decision.DENY,
    "jarn's own secret store is never accessible to the agent",
    dangerous=True,
    block_remember_always=True,
)


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
    #: True when the execution backend formats READ paths in a VIRTUAL namespace
    #: rooted at ``project_root`` (the local backend's ``virtual_mode=True``): a
    #: grep header / read_file target ``/secrets/notes.txt`` then denotes
    #: ``<project_root>/secrets/notes.txt`` on the host, NOT host-absolute ``/``.
    #: When set, READ targets are canonicalized to host identity
    #: (:meth:`_canonical_read_target`) at every entry — the pre-exec gate AND the
    #: result filter — so a relative sensitive-glob/deny matches by file identity
    #: and a remembered allow of the displayed path takes effect. OFF (default) for
    #: docker/sandbox (real host/container paths) — no guessing, byte-identical
    #: behavior. This is READ-only; command/write matching is untouched.
    virtual_reads: bool = False
    #: Optional sink for ALWAYS-scoped rules so they persist across processes
    #: (wired by the controller to a :class:`PermissionRuleStore`).
    persist: Callable[[str], object] | None = None
    _session_allow: list[str] = field(default_factory=list)
    _session_deny: list[str] = field(default_factory=list)

    # -- public API ---------------------------------------------------------

    def evaluate(self, action: Action) -> PermissionResult:
        # Map any virtual READ targets to host identity ONCE up front, so the deny →
        # allow → sensitive precedence below runs over one stable candidate set.
        action = self._canonicalized_action(action)
        guard = self._guard_for(action)

        if guard.level is GuardLevel.BLOCKED:
            return PermissionResult(
                Decision.DENY, f"blocked by danger-guard: {guard.reason}",
                dangerous=True, block_remember_always=True,
            )

        if self._touches_secret_store(action):
            return _SECRET_STORE_DENIAL

        if self._matches(action, self._all_deny()):
            return PermissionResult(Decision.DENY, "matched a deny rule")

        if guard.level is GuardLevel.DANGEROUS:
            # Force confirmation regardless of mode/allowlist; never auto-allow,
            # never persist an ALWAYS rule for it.
            return PermissionResult(
                Decision.ASK, f"danger-guard: {guard.reason}",
                dangerous=True, block_remember_always=True,
            )

        if action.kind is ActionKind.READ:
            # Reads auto-ALLOW except SENSITIVE candidates, which ASK — but each
            # candidate is judged on its own: an allow rule matching a BENIGN
            # candidate must NOT mask a DIFFERENT sensitive one (e.g. an allow on
            # the search scope ``/repo`` cannot suppress the sensitive ``**/.env``
            # glob of the same grep). A sensitive candidate is cleared only by an
            # allow matching THAT candidate (the explicit escape hatch).
            return self._read_decision(action)

        if self._matches(action, self._all_allow()):
            return PermissionResult(Decision.ALLOW, "matched an allow rule")

        return self._mode_decision(action)

    def _read_decision(self, action: Action) -> PermissionResult:
        """READ verdict (deny already cleared): ASK if ANY candidate is sensitive and
        NOT covered by an allow matching that same candidate; otherwise ALLOW. Reads
        are always permitted (any mode) unless an un-allowed sensitive candidate
        forces confirmation — an allow on another candidate cannot mask it."""
        allow = self._applicable_patterns(action, self._all_allow())
        for cand in self._read_candidates(action):
            if self._is_sensitive_read_canonical(cand) and not self._read_candidate_matches(
                cand, allow
            ):
                return PermissionResult(
                    Decision.ASK, "sensitive-path read requires confirmation"
                )
        return PermissionResult(Decision.ALLOW, "reads are always permitted")

    def remember(self, action: Action, scope: RememberScope) -> str | None:
        """Record an approval. For ALWAYS, also persist the rule via
        :attr:`persist` (if wired) and return it; otherwise return ``None``.

        SESSION and ALWAYS both take effect immediately (in-memory allowlist);
        ALWAYS additionally survives across processes through ``persist``.
        """
        rule = self._rule_for(action)
        if scope is RememberScope.ONCE:
            return None
        scoped_rule = self._scoped_rule_for(action, rule)
        if scoped_rule not in self._session_allow:
            self._session_allow.append(scoped_rule)
        if scope is RememberScope.ALWAYS:
            if self.persist is not None:
                try:
                    self.persist(scoped_rule)
                except (ConfigCorruptError, OSError) as exc:
                    # The in-memory allow still applies for this session; only the
                    # cross-process persistence is skipped. ConfigCorruptError means
                    # the project config is unreadable and the user sees the repair
                    # hint at the next load. OSError is the I/O half — a read-only
                    # or full disk, or a lost race on the config file — and it must
                    # be caught HERE: uncaught it escaped `remember` into
                    # `_stream_turn` → `run_turn` and killed the turn outright.
                    _log.warning("Could not persist allow-rule: %s", exc)
            return rule
        return None

    def deny_session(self, action: Action) -> None:
        rule = self._rule_for(action)
        scoped_rule = self._scoped_rule_for(action, rule)
        if scoped_rule not in self._session_deny:
            self._session_deny.append(scoped_rule)

    def remember_scope_summary(self, action: Action) -> str:
        """Human-readable scope that would be remembered for *action*."""
        workspace = self._workspace_scope() or "no project workspace"
        tool = action.tool or "direct action"
        target = self._rule_for(action)
        noun = "command prefix" if action.kind is ActionKind.SHELL else "target"
        return (
            f"capability {action.kind.value} via {tool}; {noun} {target!r}; "
            f"workspace {workspace}"
        )

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
        """True when a filesystem *path* is hard-denied for reading: it lives in
        jarn's own secret store, or it matches an explicit read *deny* rule
        (config ``rules.deny`` or a session deny).

        Defense-in-depth backstop for ``read_file``: a denied read is already
        blocked pre-exec, but the result-filter re-checks so a denied file's
        contents can never reach the model even if that gate is bypassed."""
        act = self._canonicalized_action(Action(ActionKind.READ, target=path))
        return self._touches_secret_store(act) or self._matches(act, self._all_deny())

    def read_content_blocked(self, path: str) -> bool:
        """True when a file at *path* must not have its CONTENTS surfaced by a
        broad read tool (``grep``): it matches a read *deny* rule OR a
        sensitive-read glob, and is NOT covered by an explicit *allow* rule.

        Mirrors :meth:`evaluate`'s precedence (deny > allow > sensitive-read) so
        the result-filter and the pre-exec gate agree: a broad ``grep`` over a
        benign scope silently drops hits from ``.env``/keys (the exfiltration the
        gate cannot catch), while an explicitly allow-listed secret path still
        comes through."""
        # Canonicalize the path ONCE, then run secret-store → deny → allow → sensitive
        # over that one canonical action (mirrors evaluate()'s precedence for the
        # result filter). The secret store leads and ignores allow rules, exactly as
        # in evaluate(): a broad grep must never surface a stored credential, and no
        # allow rule may unlock it.
        act = self._canonicalized_action(Action(ActionKind.READ, target=path))
        if self._touches_secret_store(act):
            return True
        if self._matches(act, self._all_deny()):
            return True
        if self._matches(act, self._all_allow()):
            return False
        return self._is_sensitive_read(act)

    # -- jarn's own secret store (hard floor) --------------------------------
    #
    # ``~/.jarn/secrets/<service>/<account>`` holds the provider API keys behind
    # every ``file:`` secret reference — the store ``jarn login`` falls back to when
    # no OS keychain is available. Those files carry no extension and are named after
    # the account, so none of the ``sensitive_read_globs`` shapes (``*.pem``,
    # ``**/*.key``, ``**/id_*``) ever described them. Worse, the store is routinely
    # IN SCOPE: ``find_project_root`` returns ``$HOME`` for any launch directory under
    # it that is not itself a project, which puts ``~/.jarn/`` inside the agent's
    # readable tree. Reading a key and shipping it out through an allowed network tool
    # is the exact exfiltration path ``sensitive_read_globs`` exists to prevent.
    #
    # There is no legitimate agent access to this directory, so it is a hard DENY
    # rather than an ASK: an approval prompt is weak protection when the agent
    # authored the request. It is enforced above the allow tier and is not
    # configurable — deliberately not an entry in ``sensitive_read_globs``, which
    # routes to ASK and can be emptied.

    def _touches_secret_store(self, action: Action) -> bool:
        """True when *action* reads from or writes into jarn's own secret store.

        READ is judged over EVERY candidate (``target`` plus a grep/glob's extra
        ``read_targets``), so a benign search scope cannot mask a ``glob`` narrowed
        onto the store. WRITE is judged on its target — the agent has no business
        clobbering stored credentials either. SHELL and NETWORK are out of scope
        here: a shell command is gated by the danger-guard and the coarse mode, and
        this predicate is about path-addressed tool access.

        Expects an ALREADY-canonical action (:meth:`_canonicalized_action`), so a
        virtual-namespace read is judged by host identity.
        """
        if action.kind is ActionKind.READ:
            return any(self._is_secret_store_path(c) for c in self._read_candidates(action))
        if action.kind is ActionKind.WRITE:
            if self._is_secret_store_path(action.target):
                return True
            # A WRITE target is NOT canonicalized by :meth:`_canonicalized_action`
            # (that is READ-only, by design), so under the local backend's virtual
            # namespace ``/x`` still denotes ``<project_root>/x`` on the host here.
            # Judge that host identity too, or a ``JARN_HOME`` nested in the project
            # root and spelled anything other than ``.jarn`` — which is exactly what
            # ``action/action.yml`` configures — is covered only by the lexical glob,
            # and therefore not at all.
            if self.virtual_reads:
                return self._is_secret_store_path(self._canonical_read_target(action.target))
        return False

    def _is_secret_store_path(self, path: str) -> bool:
        """True when *path* names, or falls under, a jarn secret-store directory.

        Two independent checks, either of which is sufficient:

        1. **Identity** — the path resolved to an absolute location that IS, or sits
           beneath, one of :func:`~jarn.config.paths.secret_store_dirs`. Because both
           sides are ``resolve()``d, a symlink pointing into the store is caught by
           the file it names, not by how it is spelled. This is the primary check and
           it follows ``$JARN_HOME``.
        2. **Lexical** — one of :data:`_SECRET_STORE_GLOBS` matches the path's
           canonical aliases. This covers a GLOB candidate, which has no file to
           resolve, and a host where no store directory could be located.

        Never raises: any resolution failure falls through to the lexical check.
        """
        if not path:
            return False
        resolved = self._resolve_absolute(path)
        if resolved is not None:
            from jarn.config.paths import secret_store_dirs

            if any(_path_within(resolved, store) for store in secret_store_dirs()):
                return True
        aliases, _ = self._read_alias_set(path)
        if resolved is not None:
            # Fold the resolved identity into the lexical alias set too. With no
            # configured root ``_read_alias_set`` derives no identity of its own, so
            # a SYMLINK into the store would otherwise reach this check under its own
            # innocent spelling and pass. Adding an alias only ever WIDENS the deny.
            aliases = {*aliases, resolved.as_posix()}
        return any(
            fnmatch.fnmatch(alias.casefold(), pattern)
            for pattern in _SECRET_STORE_GLOBS
            for alias in aliases
        )

    def _resolve_absolute(self, path: str) -> Path | None:
        """Resolved-absolute identity of a CONCRETE path, for the secret-store check.

        Unlike :meth:`_resolved_read_path` this does not require a configured root —
        the store must stay off-limits when jarn runs outside a project — and it
        expands a leading ``~`` before anchoring, so the ``~/.jarn/secrets/...``
        spelling a model is most likely to produce resolves like any other. A
        relative path is anchored at the primary root when there is one, else at the
        process CWD. Returns ``None`` for a glob (resolving would destroy its
        metacharacters) or when the filesystem cannot answer.
        """
        if _is_glob(path):
            return None
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                roots = self._scope_roots()
                if roots:
                    candidate = roots[0].resolve() / candidate
            return candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return None

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
        # READ actions never reach here — :meth:`evaluate` routes them to
        # :meth:`_read_decision` (per-candidate sensitive/allow) after the deny +
        # danger-guard checks. This handles WRITE/SHELL/NETWORK by coarse mode.
        mode = self.mode
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
        # READ rules are stored/matched by HOST identity: canonicalize a virtual
        # target so a remembered allow / session deny of the DISPLAYED path takes
        # effect against the same file's host identity later (result filter). This
        # is what makes the explicit-allow escape hatch work under virtual_mode and
        # keeps deny>allow>sensitive a single, precedence-correct decision.
        if action.kind is ActionKind.READ:
            return self._canonical_read_target(action.target)
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
            self._is_sensitive_read_canonical(cand)
            for cand in self._read_candidates(action)  # already host-canonical
        )

    def _canonicalized_action(self, action: Action) -> Action:
        """Return ``action`` with its READ targets mapped to host identity EXACTLY
        ONCE, so one precedence decision (deny → allow → sensitive) runs over a single
        stable canonical candidate set — no repeated per-check ``resolve()``. Applied
        at each public READ entry (:meth:`evaluate`, :meth:`read_content_blocked`,
        :meth:`is_read_denied_path`); non-READ actions and the no-op modes
        (``virtual_reads`` off) are returned unchanged."""
        if action.kind is not ActionKind.READ or not self.virtual_reads:
            return action
        # Memoize by raw spelling: tool_to_action stores the primary path in BOTH
        # ``target`` and ``read_targets[0]``, so map each DISTINCT raw path once.
        cache: dict[str, str] = {}

        def canon(raw: str) -> str:
            if raw not in cache:
                cache[raw] = self._canonical_read_target(raw)
            return cache[raw]

        target = canon(action.target)
        read_targets = tuple(canon(t) for t in action.read_targets)
        if target == action.target and read_targets == action.read_targets:
            return action
        return replace(action, target=target, read_targets=read_targets)

    def _read_candidates(self, action: Action) -> tuple[str, ...]:
        """The path-like targets a READ is judged against: the primary ``target`` plus
        any extra ``read_targets`` (a grep/glob ``glob`` value), de-duplicated with
        empties dropped (order preserved). The action's targets are ALREADY
        host-canonical — mapped once by :meth:`_canonicalized_action` at the public
        entry — so this does NOT re-map (that would resolve the same path repeatedly)."""
        out: list[str] = []
        for cand in (action.target, *action.read_targets):
            if cand and cand not in out:
                out.append(cand)
        return tuple(out)

    def _canonical_read_target(self, target: str) -> str:
        """Map ONE backend-namespace READ path to host identity.

        CONTRACT: called EXACTLY ONCE per raw path, and only on a path in the
        execution backend's namespace — a virtual grep/read target or its glob
        candidate, supplied by the pre-exec bridge or the result filter. It is never
        given its own output, nor an arbitrary host path the engine picked. Active
        only when :attr:`virtual_reads` is set (the local backend formats read/grep
        paths as VIRTUAL-absolute, rooted at ``project_root``: ``/x`` means
        ``<project_root>/x`` on the host).

        - A real host-absolute path inside an added ``--add-dir`` root — which the
          local backend emits UNCHANGED — is kept as-is (round-7 #3: never rebased,
          so an unrelated primary-root rule can't falsely redact an added-root read).
        - Every OTHER absolute concrete target is VIRTUAL and is rebased under
          ``project_root``. We deliberately do NOT exempt "already under
          ``project_root``": in virtual mode the backend never emits a primary-tree
          file by its host spelling (it shows ``/x``), so a string that happens to
          resolve under ``project_root`` is a virtual path and MUST rebase — closing
          the spelling-collision hole where the engine and backend disagreed on which
          file a name denotes (round-7 #2). Because canonicalization runs exactly
          once, this needs no idempotence exemption.
        - A virtual-absolute GLOB candidate (grep's ``glob`` narrowed to
          ``/secrets/*.txt``) is rebased LEXICALLY to its project-relative spelling
          so it meets a relative sensitive rule — it can't be ``resolve()``d without
          destroying the metacharacters (round-7 #1).
        - A relative target, and everything when ``virtual_reads`` is off or there is
          no root anchor, is returned unchanged — no guessing."""
        if not self.virtual_reads or not target or self.project_root is None:
            return target
        if not target.startswith("/"):
            return target
        if _is_glob(target):
            return target.lstrip("/")
        try:
            resolved = Path(target).resolve()
        except (OSError, RuntimeError, ValueError):
            resolved = Path(target)
        for root in self.roots:  # ADDED roots only — backend keeps these as host paths
            try:
                r = root.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved == r or r in resolved.parents:
                return target
        return (self.project_root / target.lstrip("/")).as_posix()

    def is_sensitive_read_path(self, path: str) -> bool:
        """True when a filesystem *path* matches a configured sensitive-read glob.

        The SINGLE source of truth shared by the READ mode-decision here and the
        result-filter middleware (:mod:`jarn.agent.read_filter`).

        Matching runs against the path's canonical ALIASES (:meth:`_read_alias_set`):
        the raw normalized path, a leading-slash form (so a ``**/``-anchored pattern
        also catches a bare relative target, ``.env`` vs ``**/.env``, WITHOUT the
        false positives a basename-only match would create), AND — when a
        ``project_root`` anchor is configured — the resolved-absolute and
        project-relative forms of the SAME file. That closes the spelling gap where a
        RELATIVE glob (``secrets/*.txt``) never met the ABSOLUTE grep-result header
        (``/proj/secrets/notes.txt``) for the same file. ``fnmatch``'s ``*`` spans
        ``/``, so ``*.pem`` matches a .pem file at any depth. An empty
        ``sensitive_read_globs`` disables the check entirely.
        """
        return self._is_sensitive_read_canonical(self._canonical_read_target(path))

    def _is_sensitive_read_canonical(self, path: str) -> bool:
        """Sensitive-glob check on an ALREADY host-canonical READ path — no further
        canonicalization. Internal callers pass canonical ``_read_candidates`` so a
        backend-namespace path is mapped to host identity EXACTLY ONCE (never
        chained); the public :meth:`is_sensitive_read_path` canonicalizes first."""
        globs = self.rules.sensitive_read_globs
        if not globs or not path:
            return False
        aliases, _ = self._read_alias_set(path)
        return any(
            fnmatch.fnmatch(alias, pattern)
            for pattern in self._expand_read_patterns(globs)
            for alias in aliases
        )

    def _matches(self, action: Action, patterns: list[str]) -> bool:
        # READ targets are matched by FILE IDENTITY (:meth:`_read_candidate_matches`)
        # so a rule/glob written in one spelling catches the same file named in
        # another (relative vs absolute). COMMAND/WRITE/NETWORK matching is left
        # byte-identical — its scope/symlink gating (guard + ``_in_scope``) is
        # separate and unchanged.
        applicable = self._applicable_patterns(action, patterns)
        if action.kind is ActionKind.READ:
            return any(
                self._read_candidate_matches(cand, applicable)
                for cand in self._read_candidates(action)
            )
        candidates = {action.target, self._rule_for(action)}
        for pattern in applicable:
            for cand in candidates:
                if cand == pattern or fnmatch.fnmatch(cand, pattern):
                    return True
        return False

    def _workspace_scope(self) -> str:
        roots = self._scope_roots()
        if not roots:
            return ""
        try:
            return roots[0].resolve().as_posix()
        except (OSError, RuntimeError, ValueError):
            # An unresolved workspace is still scoped to its explicit spelling;
            # it must never degrade to the unscoped legacy-rule behavior.
            return roots[0].absolute().as_posix()

    def _scoped_rule_for(self, action: Action, rule: str) -> str:
        payload = {
            "kind": action.kind.value,
            "rule": rule,
            "tool": action.tool or "",
            "workspace": self._workspace_scope(),
        }
        return _SCOPED_RULE_PREFIX + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )

    def _applicable_patterns(self, action: Action, patterns: list[str]) -> list[str]:
        """Unwrap only scoped rules whose full authority matches this action."""
        applicable: list[str] = []
        workspace = self._workspace_scope()
        tool = action.tool or ""
        for raw in patterns:
            if not raw.startswith(_SCOPED_RULE_PREFIX):
                applicable.append(raw)  # explicit legacy/manual config rule
                continue
            try:
                payload = json.loads(raw[len(_SCOPED_RULE_PREFIX) :])
            except (TypeError, ValueError):
                continue  # malformed generated rule fails closed
            if not isinstance(payload, dict):
                continue
            if set(payload) != {"kind", "rule", "tool", "workspace"}:
                continue
            if payload.get("kind") != action.kind.value:
                continue
            if payload.get("tool") != tool or payload.get("workspace") != workspace:
                continue
            rule = payload.get("rule")
            if isinstance(rule, str) and rule:
                applicable.append(rule)
        return applicable

    # -- READ-path identity matching (relative/absolute alias unification) ---
    #
    # A concrete READ path is matched by FILE IDENTITY, not lexical spelling: a
    # relative ``sensitive_read_glob``/deny/session-rule must catch the ABSOLUTE
    # grep-result header for the same file, and vice-versa. Every concrete path is
    # reduced to canonical aliases ONCE (:meth:`_read_alias_set`) and those aliases
    # are applied consistently to sensitive globs, allow rules, deny rules, and
    # session rules. This is READ-only — command/write gating is untouched.

    def _read_alias_set(self, path: str) -> tuple[set[str], Path | None]:
        """Canonical aliases of a READ ``path`` + its resolved-absolute identity.

        Aliases: (a) the normalized caller form and a leading-slash variant (so a
        ``**/``-anchored glob catches a bare relative name), and — when a
        ``project_root``/added root is configured — (b) the resolved-absolute form
        anchored at the primary root (file identity, symlinks + ``..`` collapsed)
        and (c) the project-relative form when the file is inside an active root.

        A ``path`` that is itself a GLOB (a grep/glob ``glob`` candidate such as
        ``**/.env``) is a pattern, not a concrete file: it keeps ONLY the lexical
        aliases (resolving it would be meaningless) and has no identity. With no
        root anchor, (b)/(c) are skipped so matching stays byte-identical to the
        pre-fix lexical behavior.
        """
        norm = path.replace("\\", "/")
        aliases = {norm, norm if norm.startswith("/") else "/" + norm}
        if _is_glob(path):
            return aliases, None
        identity = self._resolved_read_path(path)
        if identity is not None:
            aliases.add(identity.as_posix())
            rel = self._project_relative(identity)
            if rel is not None:
                aliases.add(rel)
        return aliases, identity

    def _resolved_read_path(self, path: str) -> Path | None:
        """Resolved-absolute identity of a concrete READ ``path``, anchored at the
        PRIMARY root (mirrors :meth:`_in_scope`'s anchoring) so a relative caller
        spelling resolves to the SAME file a later absolute grep header names.

        Returns ``None`` when there is no anchor (no configured root) or resolution
        fails — callers then fall back to the lexical aliases only, preserving the
        pre-fix behavior. This RESOLVES a concrete path; it is never called on a
        glob pattern (that would destroy the metacharacters)."""
        roots = self._scope_roots()
        if not roots:
            return None
        try:
            primary = roots[0].resolve()
            return (primary / path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None

    def _project_relative(self, resolved: Path) -> str | None:
        """The already-resolved READ target RELATIVE to the active root it falls
        under (primary or added), as a POSIX string, so a relative pattern matches
        an absolute file for the same path. ``None`` when outside every root."""
        for root in self._scope_roots():
            try:
                r = root.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved == r or r in resolved.parents:
                try:
                    return resolved.relative_to(r).as_posix()
                except ValueError:
                    continue
        return None

    def _expand_read_patterns(self, patterns: list[str]) -> list[str]:
        """READ ``patterns`` plus a dot-segment-normalized POSIX alias for each, so a
        DOT-RELATIVE rule/glob (``./secrets/*.txt``, ``a/./b``) matches the same file
        a physical path names (round-5 #2). Only when a root anchor is configured —
        with no root the patterns are returned unchanged so matching stays
        byte-identical to the pre-fix lexical behavior (``project_root=None``).
        ``PurePosixPath`` collapses ``.``/``./`` segments while preserving glob
        metacharacters (``*``/``**``) and ``..`` (which needs the filesystem to
        resolve and is left to the identity path)."""
        if not self._scope_roots():
            return patterns
        out = list(patterns)
        seen = set(patterns)
        for p in patterns:
            if not p:
                continue
            norm = PurePosixPath(p).as_posix()
            if norm and norm != "." and norm not in seen:
                out.append(norm)
                seen.add(norm)
        return out

    def _read_candidate_matches(self, cand: str, patterns: list[str]) -> bool:
        """True when a READ candidate matches ANY allow/deny ``pattern`` by file
        identity. Each of the candidate's aliases is tested against every pattern
        (a GLOB pattern keeps its metacharacters); additionally, a CONCRETE pattern
        is compared by resolved-absolute identity, so a relative session-deny
        (``./secrets/notes.txt``) catches an absolute grep header for the same file
        and vice-versa. A glob candidate has no identity, so only its lexical
        aliases apply."""
        aliases, identity = self._read_alias_set(cand)
        for pattern in self._expand_read_patterns(patterns):
            for alias in aliases:
                if alias == pattern or fnmatch.fnmatch(alias, pattern):
                    return True
            if identity is not None and not _is_glob(pattern):
                pat_identity = self._resolved_read_path(pattern)
                if pat_identity is not None and pat_identity == identity:
                    return True
        return False

    def _all_allow(self) -> list[str]:
        return [*self.rules.allow, *self._session_allow]

    def _all_deny(self) -> list[str]:
        return [*self.rules.deny, *self._session_deny]
