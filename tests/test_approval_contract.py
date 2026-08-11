"""Remembered approvals never widen across capability, tool, or workspace."""

from __future__ import annotations

import json

from jarn.config.schema import PermissionMode, PermissionRules
from jarn.permissions import (
    Action,
    ActionKind,
    Decision,
    PermissionEngine,
    RememberScope,
)


def test_persisted_approval_has_complete_scope_and_no_cross_capability_widening(
    tmp_path,
) -> None:
    workspace = tmp_path / "project-a"
    workspace.mkdir()
    persisted: list[str] = []
    engine = PermissionEngine(
        mode=PermissionMode.ASK,
        project_root=workspace,
        persist=persisted.append,
    )
    approved = Action(ActionKind.SHELL, "npm test -- --runInBand", tool="execute")

    human_rule = engine.remember(approved, RememberScope.ALWAYS)

    assert human_rule == "npm test"
    assert len(persisted) == 1
    raw = persisted[0]
    assert raw.startswith("jarn-scope:v1:")
    scope = json.loads(raw.removeprefix("jarn-scope:v1:"))
    assert scope == {
        "kind": "shell",
        "rule": "npm test",
        "tool": "execute",
        "workspace": workspace.resolve().as_posix(),
    }
    assert engine.evaluate(approved).decision is Decision.ALLOW
    assert (
        engine.evaluate(Action(ActionKind.SHELL, "npm test -- --watch", tool="execute")).decision
        is Decision.ALLOW
    )
    assert (
        engine.evaluate(Action(ActionKind.SHELL, "npm publish", tool="execute")).decision
        is Decision.ASK
    )
    assert (
        engine.evaluate(Action(ActionKind.SHELL, "npm test", tool="hook")).decision
        is Decision.ASK
    )
    assert (
        engine.evaluate(Action(ActionKind.WRITE, "npm test", tool="execute")).decision
        is Decision.ASK
    )


def test_persisted_approval_does_not_follow_rule_into_another_workspace(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    persisted: list[str] = []
    first_engine = PermissionEngine(
        mode=PermissionMode.ASK,
        project_root=first,
        persist=persisted.append,
    )
    action = Action(ActionKind.SHELL, "pytest -q", tool="execute")
    first_engine.remember(action, RememberScope.ALWAYS)

    second_engine = PermissionEngine(
        mode=PermissionMode.ASK,
        project_root=second,
        rules=PermissionRules(allow=persisted),
    )
    assert second_engine.evaluate(action).decision is Decision.ASK


def test_malformed_generated_scope_fails_closed(tmp_path) -> None:
    engine = PermissionEngine(
        mode=PermissionMode.ASK,
        project_root=tmp_path,
        rules=PermissionRules(allow=["jarn-scope:v1:{not-json"]),
    )
    assert (
        engine.evaluate(Action(ActionKind.SHELL, "jarn-scope:v1:{not-json")).decision
        is Decision.ASK
    )


def test_legacy_manual_rules_remain_backward_compatible(tmp_path) -> None:
    engine = PermissionEngine(
        mode=PermissionMode.ASK,
        project_root=tmp_path,
        rules=PermissionRules(allow=["git status"]),
    )
    assert (
        engine.evaluate(Action(ActionKind.SHELL, "git status --short", tool="execute")).decision
        is Decision.ALLOW
    )
