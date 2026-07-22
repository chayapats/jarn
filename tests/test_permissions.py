"""Permission engine tests — modes, rules, danger-guard precedence, remember."""

from __future__ import annotations

import pytest

from jarn.config.schema import PermissionMode, PermissionRules
from jarn.permissions import (
    Action,
    ActionKind,
    Decision,
    PermissionEngine,
    RememberScope,
)


def _engine(mode=PermissionMode.ASK, **kw) -> PermissionEngine:
    return PermissionEngine(mode=mode, **kw)


def test_reads_always_allowed():
    for mode in PermissionMode:
        eng = _engine(mode)
        r = eng.evaluate(Action(ActionKind.READ, "any.py"))
        assert r.decision is Decision.ALLOW


# -- Sensitive-path READ gating (secret-exfiltration defense) ---------------
#
# A read of a secret store (.env / ssh key / aws creds / private key) must be
# CONFIRMED, not silently auto-allowed — otherwise the agent can read the file
# and ship its contents out through an allowed network tool with no gate. Normal
# reads keep the fast auto-ALLOW (no approval flood).


def test_sensitive_read_asks_in_every_mode():
    """A read matching a default sensitive glob is ASK in every mode (even YOLO).

    This is the exfiltration gap: before the fix these all auto-ALLOWed.
    """
    sensitive = [
        ".env", "config/.env", ".env.local", "/proj/.env.local",
        "/home/u/.ssh/id_rsa", ".ssh/id_rsa", "id_rsa",
        "/proj/.aws/credentials", ".aws/credentials",
        "/proj/.git/config", "server.pem", "/proj/certs/server.pem",
        "server.key", "/proj/id_ed25519", "id_ed25519",
    ]
    for mode in PermissionMode:
        eng = _engine(mode)
        for path in sensitive:
            r = eng.evaluate(Action(ActionKind.READ, path))
            assert r.decision is Decision.ASK, f"{mode.value} {path} -> {r.decision}"


def test_normal_reads_still_auto_allowed():
    """Non-sensitive reads keep the fast auto-ALLOW — no approval flood.

    Includes near-miss false-positive traps: a plain ``config`` must NOT match
    ``**/.git/config``; a plain ``credentials`` must NOT match ``**/.aws/credentials``.
    """
    eng = _engine(PermissionMode.ASK)
    for path in (
        "src/app.py", "README.md", "config", "credentials",
        "any.py", ".github/config", "notes.txt",
    ):
        r = eng.evaluate(Action(ActionKind.READ, path))
        assert r.decision is Decision.ALLOW, f"{path} -> {r.decision}"


def test_explicit_deny_rule_enforced_for_reads():
    """(fix b) A ``permissions.deny`` rule blocks a read once it reaches the engine.

    Deny takes precedence over the sensitive-glob ASK and over the mode.
    """
    eng = _engine(PermissionMode.YOLO, rules=PermissionRules(deny=["**/secrets.txt"]))
    assert eng.evaluate(
        Action(ActionKind.READ, "/proj/secrets.txt")
    ).decision is Decision.DENY
    # Deny wins over the sensitive-glob ASK too.
    eng2 = _engine(PermissionMode.YOLO, rules=PermissionRules(deny=["**/.env"]))
    assert eng2.evaluate(
        Action(ActionKind.READ, "/proj/.env")
    ).decision is Decision.DENY


def test_allow_rule_overrides_sensitive_read_ask():
    """An explicit allow rule opts a specific secret path back into ALLOW."""
    eng = _engine(PermissionMode.ASK, rules=PermissionRules(allow=[".env"]))
    assert eng.evaluate(Action(ActionKind.READ, ".env")).decision is Decision.ALLOW


def test_sensitive_read_globs_configurable_off():
    """Empty ``sensitive_read_globs`` disables the extra gating (opt-out)."""
    eng = _engine(PermissionMode.ASK, rules=PermissionRules(sensitive_read_globs=[]))
    assert eng.evaluate(Action(ActionKind.READ, ".env")).decision is Decision.ALLOW


def test_sensitive_read_globs_default_populated():
    """The default rules ship the sensitive-path glob list (default-safe)."""
    assert PermissionRules().sensitive_read_globs  # non-empty by default


# -- Multi-candidate READ gating (grep/glob ``glob`` arg) -------------------
#
# A grep/glob is judged against its search ``path`` AND its ``glob`` — a benign
# path must not mask a sensitive glob (Codex second-eye #1).


def test_read_extra_candidate_glob_triggers_sensitive_ask():
    """A benign primary target with a sensitive ``read_targets`` candidate ASKs."""
    eng = _engine(PermissionMode.YOLO)
    action = Action(ActionKind.READ, "/repo", read_targets=("/repo", "**/.env"))
    assert eng.evaluate(action).decision is Decision.ASK


def test_read_extra_candidate_glob_triggers_deny():
    """A read-deny on the glob candidate wins even when the primary is benign."""
    eng = _engine(PermissionMode.YOLO, rules=PermissionRules(deny=["**/.env"]))
    action = Action(ActionKind.READ, "/repo", read_targets=("/repo", "**/.env"))
    assert eng.evaluate(action).decision is Decision.DENY


# -- Read-result-filter predicates (used by jarn.agent.read_filter) ---------


def test_read_content_blocked_matches_sensitive_and_deny():
    """The predicate the grep result-filter uses: sensitive globs AND explicit
    deny both block a matched file's contents; ordinary files pass."""
    eng = _engine(PermissionMode.YOLO, rules=PermissionRules(deny=["**/secret.txt"]))
    assert eng.read_content_blocked("/proj/.env") is True            # sensitive glob
    assert eng.read_content_blocked("/proj/id_rsa") is True          # sensitive glob
    assert eng.read_content_blocked("/proj/conf/secret.txt") is True  # deny rule
    assert eng.read_content_blocked("/proj/src/app.py") is False     # ordinary


def test_read_content_blocked_respects_allow_optout():
    """An explicit allow rule opts a secret path back in (deny > allow > sensitive)."""
    eng = _engine(PermissionMode.ASK, rules=PermissionRules(allow=["/proj/.env"]))
    assert eng.read_content_blocked("/proj/.env") is False


def test_read_content_blocked_deny_beats_allow():
    eng = _engine(
        PermissionMode.ASK,
        rules=PermissionRules(allow=["/proj/.env"], deny=["/proj/.env"]),
    )
    assert eng.read_content_blocked("/proj/.env") is True


def test_is_read_denied_path_is_deny_only():
    """The read_file backstop is deny-only: a sensitive-but-not-denied path is
    NOT reported denied (an approved sensitive read must still come through)."""
    eng = _engine(PermissionMode.YOLO, rules=PermissionRules(deny=["**/id_rsa"]))
    assert eng.is_read_denied_path("/home/u/.ssh/id_rsa") is True
    assert eng.is_read_denied_path("/proj/.env") is False


# -- Second-eye final: READ path matching by FILE IDENTITY (relative/absolute) --
#
# READ authorization used to match LEXICAL STRINGS, so a RELATIVE sensitive glob
# or session-deny never met the ABSOLUTE grep-result header for the SAME file (and
# vice-versa) and the secret leaked. The engine now derives canonical aliases for a
# concrete read path (normalized caller form + resolved-absolute anchored at
# project_root + project-relative) and matches by file identity.


def test_relative_sensitive_glob_matches_absolute_read_path(tmp_path):
    """A RELATIVE custom sensitive glob (`secrets/*.txt`) catches an ABSOLUTE read
    path for the same file — the identity the broad-grep result-filter passes in."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "notes.txt").write_text("x")
    eng = _engine(
        PermissionMode.YOLO,
        rules=PermissionRules(sensitive_read_globs=["secrets/*.txt"]),
        project_root=tmp_path,
    )
    abs_path = str(tmp_path / "secrets" / "notes.txt")
    assert eng.read_content_blocked(abs_path) is True
    assert eng.evaluate(Action(ActionKind.READ, abs_path)).decision is Decision.ASK
    # No over-redaction: a benign sibling in the same tree is not blocked.
    assert eng.read_content_blocked(str(tmp_path / "src" / "app.py")) is False


def test_relative_session_deny_matches_absolute_read_path(tmp_path):
    """A RELATIVE session deny (`./secrets/notes.txt`) catches an ABSOLUTE read path
    for the same file (resolved-absolute identity)."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "notes.txt").write_text("x")
    eng = _engine(PermissionMode.YOLO, project_root=tmp_path)
    eng.deny_session(Action(ActionKind.READ, target="./secrets/notes.txt"))
    abs_path = str(tmp_path / "secrets" / "notes.txt")
    assert eng.is_read_denied_path(abs_path) is True
    assert eng.read_content_blocked(abs_path) is True
    assert eng.evaluate(Action(ActionKind.READ, abs_path)).decision is Decision.DENY


def test_absolute_deny_matches_relative_read_path(tmp_path):
    """The reverse direction: an ABSOLUTE deny rule catches a RELATIVE read path."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "notes.txt").write_text("x")
    abs_rule = str(tmp_path / "secrets" / "notes.txt")
    eng = _engine(
        PermissionMode.YOLO,
        rules=PermissionRules(deny=[abs_rule]),
        project_root=tmp_path,
    )
    assert eng.is_read_denied_path("secrets/notes.txt") is True
    assert eng.read_content_blocked("secrets/notes.txt") is True


def test_read_identity_matching_needs_no_root_stays_lexical(tmp_path):
    """Without a project_root anchor, matching stays purely lexical (unchanged): a
    relative glob does NOT reach an absolute path, so the alias logic is inert."""
    eng = _engine(
        PermissionMode.YOLO,
        rules=PermissionRules(sensitive_read_globs=["secrets/*.txt"]),
    )
    assert eng.read_content_blocked("/anywhere/secrets/notes.txt") is False


def test_plan_mode_denies_writes_and_shell():
    eng = _engine(PermissionMode.PLAN)
    assert eng.evaluate(Action(ActionKind.WRITE, "a.py")).decision is Decision.DENY
    assert eng.evaluate(Action(ActionKind.SHELL, "ls")).decision is Decision.DENY


def test_ask_mode_asks_for_writes_and_shell():
    eng = _engine(PermissionMode.ASK)
    assert eng.evaluate(Action(ActionKind.WRITE, "a.py")).decision is Decision.ASK
    assert eng.evaluate(Action(ActionKind.SHELL, "npm test")).decision is Decision.ASK


def test_auto_edit_allows_in_scope_writes_but_asks_shell(tmp_path):
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=tmp_path)
    in_scope = str(tmp_path / "src" / "x.py")
    assert eng.evaluate(Action(ActionKind.WRITE, in_scope)).decision is Decision.ALLOW
    assert eng.evaluate(Action(ActionKind.SHELL, "echo hi")).decision is Decision.ASK


def test_auto_edit_allows_readonly_network_but_asks_mcp():
    eng = _engine(PermissionMode.AUTO_EDIT)
    assert eng.evaluate(
        Action(ActionKind.NETWORK, "web_fetch → https://x", tool="web_fetch")
    ).decision is Decision.ALLOW
    assert eng.evaluate(
        Action(ActionKind.NETWORK, "web_search → 'gold'", tool="web_search")
    ).decision is Decision.ALLOW
    assert eng.evaluate(
        Action(ActionKind.NETWORK, "mcp/srv/act", tool="mcp__srv__act")
    ).decision is Decision.ASK
    assert eng.evaluate(
        Action(ActionKind.NETWORK, "check_async_task", tool="check_async_task")
    ).decision is Decision.ALLOW
    assert eng.evaluate(
        Action(ActionKind.NETWORK, "start_async_task", tool="start_async_task")
    ).decision is Decision.ASK


def test_auto_edit_asks_out_of_scope_write(tmp_path):
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=tmp_path)
    assert eng.evaluate(Action(ActionKind.WRITE, "/etc/hosts")).decision is Decision.ASK


# -- T-1-5: scope check is CWD-independent + symlink escape -----------------


def test_scope_cwd_independence_relative_traversal(tmp_path, monkeypatch):
    """A relative ``../outside`` write is out-of-scope regardless of process CWD.

    Before the fix, ``Path(target).resolve()`` anchored relative paths to the
    process CWD, so an agent whose shell ran from a project subdir could write
    ``../outside`` and be mis-classified as in-scope in auto-edit/yolo.
    """
    root = tmp_path / "proj"
    (root / "src" / "sub").mkdir(parents=True)
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=root)
    # Run the check from a CWD deeper than the project root.
    monkeypatch.chdir(root / "src" / "sub")
    r = eng.evaluate(Action(ActionKind.WRITE, "../outside.py"))
    assert r.decision is not Decision.ALLOW
    assert r.dangerous is True  # guard flagged write-outside-scope


def test_scope_relative_inside_project_is_allowed(tmp_path, monkeypatch):
    """A relative in-project write is allowed from any CWD (auto-edit)."""
    root = tmp_path / "proj"
    (root / "src" / "sub").mkdir(parents=True)
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=root)
    monkeypatch.chdir(root / "src" / "sub")
    # "./local.py" resolves to <root>/local.py — inside the project.
    r = eng.evaluate(Action(ActionKind.WRITE, "local.py"))
    assert r.decision is Decision.ALLOW


def test_symlink_escape_is_rejected(tmp_path):
    """A symlink inside the project pointing outside is rejected for writes."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    link = root / "src" / "escape"
    link.symlink_to(outside, target_is_directory=True)
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=root)
    r = eng.evaluate(Action(ActionKind.WRITE, str(link / "x.py")))
    assert r.decision is not Decision.ALLOW
    assert r.dangerous is True


def test_symlink_inside_project_is_allowed(tmp_path):
    """A symlink inside the project pointing elsewhere inside is in-scope."""
    root = tmp_path / "proj"
    (root / "real").mkdir(parents=True)
    (root / "src").mkdir(parents=True)
    link = root / "src" / "alias"
    link.symlink_to(root / "real", target_is_directory=True)
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=root)
    r = eng.evaluate(Action(ActionKind.WRITE, str(link / "x.py")))
    assert r.decision is Decision.ALLOW


# -- T-3-9: --add-dir multi-root scope (security battery) -------------------


def test_added_root_write_allowed(tmp_path):
    """A write inside an ADDED root is in-scope (ALLOW) in auto-edit mode.

    The engine generalizes scope from a single ``project_root`` to the primary
    root PLUS a tuple of added ``roots``; a target under any of them is in-scope.
    """
    primary = tmp_path / "primary"
    (primary / "src").mkdir(parents=True)
    added = tmp_path / "sibling"
    (added / "pkg").mkdir(parents=True)
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=primary, roots=(added,))
    # In the primary root — still allowed.
    assert eng.evaluate(
        Action(ActionKind.WRITE, str(primary / "src" / "a.py"))
    ).decision is Decision.ALLOW
    # In the ADDED root — now allowed too.
    assert eng.evaluate(
        Action(ActionKind.WRITE, str(added / "pkg" / "b.py"))
    ).decision is Decision.ALLOW


def test_outside_all_roots_denied(tmp_path):
    """A write outside the primary AND every added root is still out-of-scope."""
    primary = tmp_path / "primary"
    primary.mkdir()
    added = tmp_path / "sibling"
    added.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=primary, roots=(added,))
    r = eng.evaluate(Action(ActionKind.WRITE, str(outside / "x.py")))
    assert r.decision is not Decision.ALLOW  # ASK (out-of-scope), unchanged
    assert eng.evaluate(Action(ActionKind.WRITE, "/etc/hosts")).decision is Decision.ASK


def test_added_root_symlink_escape(tmp_path):
    """A symlink INSIDE an added root pointing OUTSIDE all roots is rejected.

    THE critical security test: the per-root ``resolve()`` symlink discipline
    must hold for added roots exactly as for the primary — following the symlink
    resolves the target out of every root, so the write is denied (and flagged
    dangerous), not allowed just because the textual path starts inside the root.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    added = tmp_path / "sibling"
    (added / "sub").mkdir(parents=True)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    link = added / "sub" / "escape"
    link.symlink_to(outside, target_is_directory=True)
    eng = _engine(PermissionMode.AUTO_EDIT, project_root=primary, roots=(added,))
    r = eng.evaluate(Action(ActionKind.WRITE, str(link / "x.py")))
    assert r.decision is not Decision.ALLOW
    assert r.dangerous is True  # guard flagged the write-outside-scope
    # A symlink inside the added root that stays inside it is fine.
    real = added / "real"
    real.mkdir()
    alias = added / "sub" / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert eng.evaluate(
        Action(ActionKind.WRITE, str(alias / "ok.py"))
    ).decision is Decision.ALLOW


def test_yolo_allows_safe_shell():
    eng = _engine(PermissionMode.YOLO)
    assert eng.evaluate(Action(ActionKind.SHELL, "npm test")).decision is Decision.ALLOW


def test_danger_guard_forces_ask_even_in_yolo():
    eng = _engine(PermissionMode.YOLO)
    r = eng.evaluate(Action(ActionKind.SHELL, "git push --force"))
    assert r.decision is Decision.ASK
    assert r.dangerous is True
    assert r.block_remember_always is True


def test_blocked_command_is_denied_uniallowlistable():
    eng = _engine(PermissionMode.YOLO, rules=PermissionRules(allow=["rm -rf"]))
    r = eng.evaluate(Action(ActionKind.SHELL, "rm -rf /"))
    assert r.decision is Decision.DENY
    assert r.block_remember_always is True


def test_deny_rule_wins_over_mode():
    eng = _engine(PermissionMode.YOLO, rules=PermissionRules(deny=["curl *"]))
    assert eng.evaluate(Action(ActionKind.SHELL, "curl http://x")).decision is Decision.DENY


def test_allow_rule_short_circuits_ask():
    eng = _engine(PermissionMode.ASK, rules=PermissionRules(allow=["npm test"]))
    assert eng.evaluate(Action(ActionKind.SHELL, "npm test")).decision is Decision.ALLOW


def test_remember_session_allows_future_calls():
    eng = _engine(PermissionMode.ASK)
    action = Action(ActionKind.SHELL, "pytest -q")
    assert eng.evaluate(action).decision is Decision.ASK
    eng.remember(action, RememberScope.SESSION)
    assert eng.evaluate(Action(ActionKind.SHELL, "pytest -q")).decision is Decision.ALLOW


def test_remember_always_returns_persisted_rule():
    eng = _engine(PermissionMode.ASK)
    rule = eng.remember(Action(ActionKind.SHELL, "npm run build"), RememberScope.ALWAYS)
    assert rule == "npm run build"  # exact script — not all npm run *


def test_remember_does_not_generalize_wrapper_commands():
    """Approving `bash -c "pytest"` must NOT allowlist every `bash -c <payload>`."""
    eng = _engine(PermissionMode.ASK)
    eng.remember(Action(ActionKind.SHELL, 'bash -c "pytest"'), RememberScope.SESSION)
    assert eng.evaluate(Action(ActionKind.SHELL, 'bash -c "pytest"')).decision is Decision.ALLOW
    # A different payload under the same wrapper is not covered.
    assert eng.evaluate(Action(ActionKind.SHELL, 'bash -c "echo pwned"')).decision is Decision.ASK


def test_remember_flag_first_command_is_exact():
    """A flag-led command (e.g. `python -c <code>`) is remembered verbatim."""
    eng = _engine(PermissionMode.ASK)
    rule = eng.remember(Action(ActionKind.SHELL, "python -c print(1)"), RememberScope.ALWAYS)
    assert rule == "python -c print(1)"  # wrapper → exact, not "python -c"


def test_remember_npm_run_script_is_exact():
    """Approving one npm script must not allowlist every other npm run script."""
    eng = _engine(PermissionMode.ASK)
    eng.remember(Action(ActionKind.SHELL, "npm run build"), RememberScope.SESSION)
    assert eng.evaluate(Action(ActionKind.SHELL, "npm run build")).decision is Decision.ALLOW
    assert eng.evaluate(Action(ActionKind.SHELL, "npm run test")).decision is Decision.ASK


def test_remember_still_generalizes_normal_subcommands():
    """Non-wrapper subcommands keep the rerun convenience (`npm test`)."""
    eng = _engine(PermissionMode.ASK)
    eng.remember(Action(ActionKind.SHELL, "npm test"), RememberScope.SESSION)
    assert eng.evaluate(Action(ActionKind.SHELL, "npm test")).decision is Decision.ALLOW


def test_remember_once_does_not_persist():
    eng = _engine(PermissionMode.ASK)
    action = Action(ActionKind.SHELL, "echo hi")
    eng.remember(action, RememberScope.ONCE)
    assert eng.evaluate(action).decision is Decision.ASK


def test_remember_always_invokes_persist_sink():
    persisted: list[str] = []
    eng = _engine(PermissionMode.ASK, persist=persisted.append)
    eng.remember(Action(ActionKind.SHELL, "npm run build"), RememberScope.ALWAYS)
    assert persisted == ["npm run build"]  # exact script + persisted
    # SESSION does not persist.
    eng.remember(Action(ActionKind.SHELL, "ls -la"), RememberScope.SESSION)
    assert persisted == ["npm run build"]


def test_rule_store_persists_across_instances(tmp_path):
    from jarn.permissions.rule_store import PermissionRuleStore

    cfg = tmp_path / ".jarn" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("permissions:\n  allow: ['git status']\n", encoding="utf-8")

    store = PermissionRuleStore(cfg)
    assert store.add_allow("npm run") is True
    assert store.add_allow("npm run") is False  # idempotent

    import yaml

    data = yaml.safe_load(cfg.read_text())
    assert data["permissions"]["allow"] == ["git status", "npm run"]


def test_rule_store_no_project_is_noop():
    from jarn.permissions.rule_store import PermissionRuleStore

    assert PermissionRuleStore(None).add_allow("anything") is False


def test_rule_store_preserves_comments(tmp_path):
    from jarn.permissions.rule_store import PermissionRuleStore

    cfg = tmp_path / ".jarn" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "# my project config\n"
        "permission_mode: ask  # default\n"
        "permissions:\n"
        "  allow:\n"
        "    - git status  # safe\n",
        encoding="utf-8",
    )
    assert PermissionRuleStore(cfg).add_allow("npm run") is True

    text = cfg.read_text()
    assert "# my project config" in text   # top comment preserved
    assert "# default" in text             # inline comment preserved
    assert "# safe" in text                # list-item comment preserved
    assert "npm run" in text               # new rule appended

    import yaml

    data = yaml.safe_load(text)
    assert data["permissions"]["allow"] == ["git status", "npm run"]
    assert data["permission_mode"] == "ask"


def test_rule_store_corrupt_not_wiped(tmp_path):
    """A corrupt project config must NOT be overwritten by add_allow; .bak saved."""
    from jarn.config.yaml_store import ConfigCorruptError
    from jarn.permissions.rule_store import PermissionRuleStore

    cfg = tmp_path / ".jarn" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    corrupt = "permissions: [oops, ,\n  allow: [unbalanced:"
    cfg.write_text(corrupt, encoding="utf-8")

    store = PermissionRuleStore(cfg)
    with pytest.raises(ConfigCorruptError, match="NOT modified"):
        store.add_allow("npm run")
    # File untouched (still corrupt, not a 1-key wipe).
    assert cfg.read_text() == corrupt
    # A backup was saved for repair.
    assert cfg.with_name(cfg.name + ".bak").is_file()


def test_rule_store_missing_file_bootstrap(tmp_path):
    """A missing project config bootstraps from {} — add_allow creates it."""
    from jarn.permissions.rule_store import PermissionRuleStore

    cfg = tmp_path / ".jarn" / "config.yaml"
    assert not cfg.exists()
    assert PermissionRuleStore(cfg).add_allow("npm run") is True
    import yaml

    data = yaml.safe_load(cfg.read_text())
    assert data["permissions"]["allow"] == ["npm run"]


def test_deny_session_blocks():
    eng = _engine(PermissionMode.YOLO)
    action = Action(ActionKind.SHELL, "rm temp.txt")
    eng.deny_session(action)
    assert eng.evaluate(action).decision is Decision.DENY


# -- Wave B: unified network egress policy on shell curl/wget ---------------


def _net_rules(allow=(), deny=()):
    from jarn.config.schema import NetworkPolicy

    return PermissionRules(network=NetworkPolicy(allow=list(allow), deny=list(deny)))


def test_network_deny_host_blocks_curl_even_in_yolo():
    """An explicit network deny is un-allowlistable (guard BLOCKED → DENY)."""
    eng = _engine(PermissionMode.YOLO, rules=_net_rules(deny=["evil.com"]))
    r = eng.evaluate(Action(ActionKind.SHELL, "curl https://evil.com"))
    assert r.decision is Decision.DENY
    assert r.block_remember_always is True


def test_network_not_allowlisted_host_asks_even_in_yolo():
    eng = _engine(PermissionMode.YOLO, rules=_net_rules(allow=["*.github.com"]))
    r = eng.evaluate(Action(ActionKind.SHELL, "curl evil.com"))
    assert r.decision is Decision.ASK
    assert r.dangerous is True


def test_network_allowlisted_host_runs_in_yolo():
    eng = _engine(PermissionMode.YOLO, rules=_net_rules(allow=["*.github.com"]))
    assert eng.evaluate(
        Action(ActionKind.SHELL, "curl https://api.github.com/x")
    ).decision is Decision.ALLOW


def test_no_network_policy_leaves_curl_unaffected():
    """Default (empty) policy → curl behaves exactly as before (YOLO allows)."""
    eng = _engine(PermissionMode.YOLO)
    assert eng.evaluate(
        Action(ActionKind.SHELL, "curl https://anything.com")
    ).decision is Decision.ALLOW


# -- Virtual-mode READ canonicalization (second-eye round-6 #1/#3) ----------
#
# The local backend presents read/grep paths in a VIRTUAL namespace rooted at
# project_root ('/x' == <root>/x on the host). When ``engine.virtual_reads`` is
# set the engine canonicalizes READ targets to host identity at EVERY entry — the
# pre-exec gate here AND the result filter — so a relative sensitive-glob/deny
# matches, the explicit-allow escape hatch works, and (round-6 #3) a real
# --add-dir path is not falsely rebased.


def test_virtual_direct_read_asks_on_relative_sensitive_glob(tmp_path):
    """round-6 #1 (pre-exec gate): a direct read_file of a VIRTUAL sensitive path
    ASKs (not silent ALLOW) when the sensitive glob is RELATIVE. Under virtual_reads
    every backend READ path is virtual-namespace, so the engine only receives the
    virtual spelling (`/secrets/notes.txt`), never the host spelling of that file."""
    proj = tmp_path / "project"
    proj.mkdir()
    eng = PermissionEngine(
        rules=PermissionRules(sensitive_read_globs=["secrets/*.txt"]),
        project_root=proj,
    )
    eng.virtual_reads = True
    assert eng.evaluate(
        Action(ActionKind.READ, target="/secrets/notes.txt")  # virtual-absolute
    ).decision is Decision.ASK


def test_virtual_reads_flag_is_load_bearing(tmp_path):
    """Without ``virtual_reads`` (docker/sandbox, and the pre-fix behavior) a virtual
    sensitive path is read as host-absolute, misses the relative glob, and is
    auto-ALLOWED — the exact leak the flag closes. Guards the fix's necessity."""
    proj = tmp_path / "project"
    proj.mkdir()
    eng = PermissionEngine(
        rules=PermissionRules(sensitive_read_globs=["secrets/*.txt"]),
        project_root=proj,
    )  # virtual_reads defaults False
    assert eng.evaluate(
        Action(ActionKind.READ, target="/secrets/notes.txt")
    ).decision is Decision.ALLOW


def test_virtual_remembered_allow_beats_sensitive_at_gate(tmp_path):
    """round-6 #1 (precedence): remembering an allow for the DISPLAYED virtual path
    makes a later evaluate of the same virtual path ALLOW — one precedence-correct
    decision (deny>allow>sensitive), stored by host identity."""
    proj = tmp_path / "project"
    proj.mkdir()
    eng = PermissionEngine(
        rules=PermissionRules(sensitive_read_globs=["secrets/*.txt"]),
        project_root=proj,
    )
    eng.virtual_reads = True
    act = Action(ActionKind.READ, target="/secrets/notes.txt")
    assert eng.evaluate(act).decision is Decision.ASK
    eng.remember(act, RememberScope.SESSION)
    assert eng.evaluate(act).decision is Decision.ALLOW  # explicit allow wins


def test_virtual_canonicalization_rules(tmp_path):
    """round-7 #2/#3: ``_canonical_read_target`` rebases every VIRTUAL absolute path
    under the primary root (even one whose SPELLING falls under the host root — the
    round-7 #2 collision), but leaves a real ``--add-dir`` host path unchanged
    (round-7 #3). Globs rebase lexically; flag-off is a no-op."""
    proj = tmp_path / "project"
    proj.mkdir()
    added = tmp_path / "added"
    added.mkdir()
    eng = PermissionEngine(rules=PermissionRules(), project_root=proj, roots=(added,))
    eng.virtual_reads = True
    virt = "/secrets/notes.txt"
    host_added = str(added / "notes.txt")
    # Virtual-absolute → rebased under the primary root. The engine's canonical READ
    # identity is the POSIX form (``.as_posix()``, matching ``_read_alias_set``'s
    # ``\\`` → ``/`` normalization) — so compare against ``.as_posix()``, not
    # ``str(Path)`` (which is backslash-separated on Windows and would spuriously
    # mismatch the forward-slash canonical output).
    assert eng._canonical_read_target(virt) == (proj / "secrets" / "notes.txt").as_posix()
    # round-7 #2: a virtual path whose SPELLING resolves under the host project root
    # is STILL virtual (the backend never emits primary-tree files by host spelling)
    # and is rebased — NOT mistaken for an already-canonical host path. A virtual path
    # always starts with ``/`` (the virtual-FS root is POSIX regardless of host OS), so
    # build the collision from proj's own anchor-relative spelling rather than
    # ``str(proj / ...)`` (which on Windows is a ``C:\`` host path, not a virtual one).
    collision = "/" + (proj / "secrets" / "notes.txt").relative_to(proj.anchor).as_posix()
    assert eng._canonical_read_target(collision) == (proj / collision.lstrip("/")).as_posix()
    # round-7 #3: a real --add-dir host path is preserved (never rebased). On Windows a
    # host absolute (``C:\...``) has no leading ``/`` so the flag-on rebase never fires;
    # on POSIX the added-root exemption preserves it — the contract holds either way.
    assert eng._canonical_read_target(host_added) == host_added
    # round-7 #1: a virtual-absolute glob rebases LEXICALLY to project-relative.
    assert eng._canonical_read_target("/secrets/*.txt") == "secrets/*.txt"
    # OFF → no-op (docker/sandbox host paths pass straight through).
    eng.virtual_reads = False
    assert eng._canonical_read_target(virt) == virt


def test_virtual_glob_candidate_asks_on_relative_sensitive_glob(tmp_path):
    """round-7 #1: a grep narrowed to a VIRTUAL-absolute glob (`/secrets/*.txt`) is
    caught by a RELATIVE sensitive rule at the PRE-EXEC gate — the glob candidate is
    lexically canonicalized so the hard pre-exec control isn't lost to a best-effort
    result filter."""
    proj = tmp_path / "project"
    proj.mkdir()
    eng = PermissionEngine(
        rules=PermissionRules(sensitive_read_globs=["secrets/*.txt"]),
        project_root=proj,
    )
    eng.virtual_reads = True
    # grep(path='/', glob='/secrets/*.txt') — the glob rides in read_targets.
    act = Action(
        ActionKind.READ, target="/", tool="grep", read_targets=("/", "/secrets/*.txt")
    )
    assert eng.evaluate(act).decision is Decision.ASK
    # Without the flag the virtual-absolute glob misses the relative rule (guard).
    eng.virtual_reads = False
    assert eng.evaluate(act).decision is Decision.ALLOW


def test_virtual_spelling_collision_still_sensitive(tmp_path):
    """round-7 #2 end-to-end: a virtual path that SPELLS under the host project root
    is rebased correctly, so a sensitive rule still ASKs (it is not silently ALLOWed
    by a heuristic that mistook it for a host path)."""
    proj = tmp_path / "project"
    proj.mkdir()
    eng = PermissionEngine(
        rules=PermissionRules(sensitive_read_globs=["**/*.txt"]),
        project_root=proj,
    )
    eng.virtual_reads = True
    # A virtual path whose spelling coincides with a host path under proj.
    collision = str(proj / "notes.txt")
    assert eng.evaluate(
        Action(ActionKind.READ, target=collision)
    ).decision is Decision.ASK


def test_virtual_canonicalization_runs_once_per_decision(tmp_path):
    """round-8 #1: one evaluate()/read_content_blocked() decision maps each raw READ
    path to host identity EXACTLY ONCE — the deny/allow/sensitive checks share a single
    canonical candidate set rather than each re-resolving the path."""
    from unittest.mock import patch

    proj = tmp_path / "project"
    proj.mkdir()
    eng = PermissionEngine(
        rules=PermissionRules(sensitive_read_globs=["secrets/*.txt"]),
        project_root=proj,
    )
    eng.virtual_reads = True

    calls: list[str] = []
    real = PermissionEngine._canonical_read_target

    def spy(self, target, _real=real, _calls=calls):
        _calls.append(target)
        return _real(self, target)

    with patch.object(PermissionEngine, "_canonical_read_target", spy):
        calls.clear()
        eng.evaluate(Action(ActionKind.READ, target="/secrets/a.txt"))
        assert calls == ["/secrets/a.txt"], calls  # exactly one, not three

        calls.clear()
        eng.read_content_blocked("/secrets/a.txt")
        assert calls == ["/secrets/a.txt"], calls

        # PRODUCTION grep: tool_to_action stores the path in BOTH target and
        # read_targets[0], so each DISTINCT raw path must still map exactly once —
        # '/' is not canonicalized twice (round-9 #1).
        from jarn.agent.permissions_bridge import tool_to_action

        calls.clear()
        eng.evaluate(
            tool_to_action("grep", {"path": "/", "glob": "/secrets/*.txt", "pattern": "x"})
        )
        assert calls == ["/", "/secrets/*.txt"], calls  # '/' mapped once despite dup


def test_read_allow_on_benign_candidate_does_not_mask_sensitive(tmp_path):
    """round-9 #2: an allow rule matching a BENIGN grep candidate (the search scope
    ``/repo``) must NOT suppress the sensitive-glob gating of a DIFFERENT candidate
    (``**/.env``). Each candidate is judged on its own; the escape hatch requires an
    allow matching the SENSITIVE candidate itself."""
    from jarn.agent.permissions_bridge import tool_to_action

    # Allow the benign scope only.
    eng = _engine(PermissionMode.YOLO, rules=PermissionRules(allow=["/repo"]))
    act = tool_to_action("grep", {"path": "/repo", "glob": "**/.env", "pattern": "TOKEN="})
    assert eng.evaluate(act).decision is Decision.ASK  # sensitive glob still gated

    # Escape hatch: an allow matching the SENSITIVE candidate clears it.
    eng2 = _engine(PermissionMode.YOLO, rules=PermissionRules(allow=["/repo", "**/.env"]))
    assert eng2.evaluate(act).decision is Decision.ALLOW

    # Deny still wins over everything.
    eng3 = _engine(
        PermissionMode.YOLO, rules=PermissionRules(allow=["**/.env"], deny=["**/.env"])
    )
    assert eng3.evaluate(act).decision is Decision.DENY
