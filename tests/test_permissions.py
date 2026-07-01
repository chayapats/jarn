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
    from jarn.config._yaml_store import ConfigCorruptError
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
