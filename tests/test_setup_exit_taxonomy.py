from __future__ import annotations

import pytest

from jarn.onboarding.outcome import SetupCommandError, SetupFailureKind


@pytest.mark.parametrize(
    ("kind", "exit_code", "stable_code"),
    [
        (SetupFailureKind.CANCELLED, 130, "JARN-CLI-002"),
        (SetupFailureKind.CONFIG, 2, "JARN-CONFIG-005"),
        (SetupFailureKind.AUTH, 3, "JARN-AUTH-010"),
        (SetupFailureKind.DEPENDENCY, 9, "JARN-AUTH-001"),
        (SetupFailureKind.MODEL, 4, "JARN-MODEL-001"),
        (SetupFailureKind.NETWORK, 6, "JARN-NET-001"),
        (SetupFailureKind.TIMEOUT, 124, "JARN-NET-001"),
        (SetupFailureKind.VERIFICATION, 9, "JARN-VERIFY-001"),
        (SetupFailureKind.INTERNAL, 1, "JARN-INTERNAL-001"),
    ],
)
def test_setup_cli_preserves_failure_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: SetupFailureKind,
    exit_code: int,
    stable_code: str,
) -> None:
    def fail(**_kwargs: object) -> None:
        raise SetupCommandError("sanitized setup failure", kind=kind)

    monkeypatch.setattr("jarn.onboarding.run_setup_tui", fail)

    from jarn.cli import _cmd_setup

    assert _cmd_setup() == exit_code
    output = capsys.readouterr()
    assert output.out == ""
    assert stable_code in output.err
    for field in ("Cause:", "Component:", "Next:", "Log:"):
        assert field in output.err


def test_setup_command_error_redacts_secret_shaped_cause() -> None:
    error = SetupCommandError(
        "provider rejected sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
        kind=SetupFailureKind.AUTH,
    )

    rendered = error.detail.render()
    assert "sk-proj-" not in rendered
    assert "abcdefghijklmnopqrstuvwxyz" not in rendered
