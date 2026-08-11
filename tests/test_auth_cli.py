from __future__ import annotations

import json

import pytest

from jarn.auth import (
    AuthError,
    AuthState,
    AuthStatus,
    DependencyState,
    DependencyStatus,
)
from jarn.cli import _cmd_auth, build_parser
from jarn.codex_dependency import (
    CODEX_OFFICIAL_INSTALL_COMMAND,
    CodexDependencyInstallError,
    CodexInstallPlan,
    CodexInstallResult,
)
from jarn.errors import ErrorCode


def _status(
    dependency: DependencyState,
    *,
    ready: bool = False,
    version: str | None = None,
) -> AuthStatus:
    state = (
        AuthState.AUTHENTICATED_CHATGPT
        if ready
        else (
            AuthState.DEPENDENCY_MISSING
            if dependency is DependencyState.MISSING
            else AuthState.DEPENDENCY_INCOMPATIBLE
        )
    )
    error = None
    if not ready:
        error = AuthError(
            code=(
                ErrorCode.AUTH_DEPENDENCY_MISSING.value
                if dependency is DependencyState.MISSING
                else ErrorCode.AUTH_DEPENDENCY_INCOMPATIBLE.value
            ),
            message="Codex is unavailable",
            recovery="Run jarn auth repair",
        )
    return AuthStatus(
        state=state,
        dependency=DependencyStatus(
            state=dependency,
            executable="/old/codex" if version else None,
            version=version,
            minimum_version="0.100.0",
        ),
        checked_at="2026-08-09T00:00:00Z",
        authenticated=ready,
        ready=ready,
        auth_mode="chatgpt" if ready else None,
        plan_type="plus" if ready else None,
        error=error,
    )


def _plan() -> CodexInstallPlan:
    digest = "a" * 64
    return CodexInstallPlan(
        version="0.147.0",
        target="x86_64-unknown-linux-musl",
        asset_name="codex-package-x86_64-unknown-linux-musl.tar.gz",
        asset_url="https://releases.openai.com/codex/releases/0.147.0/package.tar.gz",
        asset_sha256=digest,
        checksum_url="https://releases.openai.com/codex/releases/0.147.0/SHA256SUMS",
        checksum_sha256="b" * 64,
        source="OpenAI Releases",
        metadata_url="https://releases.openai.com/codex/channels/latest",
        destination="/home/user/.local/bin/codex",
        release_directory="/home/user/.codex/packages/standalone/releases/0.147.0-linux",
    )


class _Installer:
    installed = False
    failure: CodexDependencyInstallError | None = None

    def resolve_plan(self):
        return _plan()

    def install(self, plan, *, on_progress=None):
        type(self).installed = True
        if on_progress:
            on_progress("verifying")
        if self.failure:
            raise self.failure
        return CodexInstallResult(
            plan=plan,
            executable=plan.destination,
            smoke_version=plan.version,
            changed=True,
            previous_version="0.1.0",
        )


def _patch_auth(monkeypatch, initial: AuthStatus, repaired: AuthStatus) -> None:
    class Service:
        def __init__(self, *, command=None):
            self.command = command

        def status(self, *, refresh=False):
            del refresh
            return repaired if self.command else initial

    _Installer.installed = False
    _Installer.failure = None
    monkeypatch.setattr("jarn.auth.CodexAuthService", Service)
    monkeypatch.setattr("jarn.auth.CodexDependencyInstaller", _Installer)


def test_auth_and_codex_aliases_expose_same_repair_contract():
    parser = build_parser()

    auth = parser.parse_args(["auth", "repair", "--yes", "--json"])
    codex = parser.parse_args(["codex", "repair", "--yes", "--json"])

    assert auth.auth_action == codex.codex_action == "repair"
    assert auth.yes is codex.yes is True
    assert auth.json is codex.json is True


def test_auth_timeout_is_visible_bounded_and_shared_by_aliases():
    parser = build_parser()

    auth = parser.parse_args(["auth", "login", "--timeout", "45"])
    codex = parser.parse_args(["codex", "status", "--timeout", "45"])

    assert auth.timeout == codex.timeout == 45.0
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["auth", "login", "--timeout", "0"])
    assert caught.value.code == 2


def test_repair_missing_dependency_decline_is_nonzero_and_actionable(monkeypatch, capsys):
    _patch_auth(
        monkeypatch,
        _status(DependencyState.MISSING),
        _status(DependencyState.COMPATIBLE, ready=True, version="0.147.0"),
    )

    rc = _cmd_auth(action="repair", as_json=True, yes=False)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert rc == 3
    assert _Installer.installed is False
    assert lines[0]["type"] == "dependency_install_offer"
    assert lines[0]["plan"]["version"] == "0.147.0"
    assert lines[1] == {
        "type": "dependency_install_declined",
        "ok": False,
        "manual_command": CODEX_OFFICIAL_INSTALL_COMMAND,
    }


@pytest.mark.parametrize(
    ("dependency", "old_version"),
    [(DependencyState.MISSING, None), (DependencyState.INCOMPATIBLE, "0.1.0")],
)
def test_repair_installs_missing_or_outdated_dependency_then_verifies_account(
    monkeypatch, capsys, dependency, old_version
):
    _patch_auth(
        monkeypatch,
        _status(dependency, version=old_version),
        _status(DependencyState.COMPATIBLE, ready=True, version="0.147.0"),
    )

    rc = _cmd_auth(action="repair", as_json=True, yes=True)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert rc == 0
    assert _Installer.installed is True
    assert [line["type"] for line in lines[:-1]] == [
        "dependency_install_offer",
        "dependency_install_result",
    ]
    assert lines[-1]["ready"] is True
    assert lines[-1]["dependency"]["state"] == "compatible"


def test_login_checks_dependency_and_declines_before_showing_login_success(monkeypatch, capsys):
    _patch_auth(
        monkeypatch,
        _status(DependencyState.MISSING),
        _status(DependencyState.COMPATIBLE, ready=True, version="0.147.0"),
    )

    rc = _cmd_auth(action="login", as_json=True)
    output = capsys.readouterr().out

    assert rc == 3
    assert "auth_challenge" not in output
    assert "authenticated_chatgpt" not in output
    assert CODEX_OFFICIAL_INSTALL_COMMAND in output


def test_repair_install_failure_is_nonzero_preserves_manual_fallback(monkeypatch, capsys):
    _patch_auth(
        monkeypatch,
        _status(DependencyState.INCOMPATIBLE, version="0.1.0"),
        _status(DependencyState.COMPATIBLE, ready=True, version="0.147.0"),
    )
    _Installer.failure = CodexDependencyInstallError("checksum", "digest mismatch")

    rc = _cmd_auth(action="repair", as_json=True, yes=True)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert rc == 3
    install_error = next(line for line in lines if line.get("type") == "dependency_install_error")
    assert install_error["stage"] == "checksum"
    assert install_error["manual_command"] == CODEX_OFFICIAL_INSTALL_COMMAND
    assert lines[-1]["ready"] is False
    assert lines[-1]["error"]["code"] == "JARN-AUTH-002"
