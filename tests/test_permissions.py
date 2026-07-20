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
