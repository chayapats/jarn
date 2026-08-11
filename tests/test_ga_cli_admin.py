"""Focused contracts for the GA administrative CLI surface."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from jarn import cli
from jarn.exit_codes import EXIT_INTERNAL


def _write_current_config(home: Path, *, secret: str | None = None) -> Path:
    path = home / "config.yaml"
    home.mkdir(parents=True, exist_ok=True)
    data = cli._template_config_mapping(project=False, path=path)
    if secret is not None:
        data["providers"]["openai"]["api_key"] = secret
    path.write_text(cli._render_config_mapping(data), encoding="utf-8")
    return path


def test_parser_exposes_ga_admin_surface_and_exit_taxonomy() -> None:
    parser = cli.build_parser()

    config = parser.parse_args(["config", "reset", "--project", "--yes", "--json"])
    assert (config.command, config.config_action, config.project, config.yes, config.json) == (
        "config",
        "reset",
        True,
        True,
        True,
    )
    doctor = parser.parse_args(
        ["doctor", "--fix", "--dry-run", "--report", "bundle.json", "--network", "--json"]
    )
    assert doctor.fix and doctor.dry_run and doctor.network and doctor.json
    assert doctor.report == "bundle.json"
    update = parser.parse_args(
        ["update", "--channel", "beta", "--dry-run", "--version", "2.0.0", "--json"]
    )
    assert (update.channel, update.dry_run, update.update_version, update.json) == (
        "beta",
        True,
        "2.0.0",
        True,
    )
    uninstall = parser.parse_args(["uninstall", "--yes", "--config", "--sessions", "--credentials"])
    assert uninstall.uninstall_config and uninstall.uninstall_sessions
    assert uninstall.uninstall_credentials
    help_text = parser.format_help()
    assert "Stable exit codes:" in help_text
    assert "3 auth" in help_text and "7 update/rollback failed" in help_text


def test_top_level_help_is_offline_complete_plain_and_non_mutating(tmp_path: Path) -> None:
    """A clean machine can learn normal operation without config or a browser."""

    guard_dir = tmp_path / "network-guard"
    guard_dir.mkdir()
    marker = tmp_path / "network-guard-loaded"
    (guard_dir / "sitecustomize.py").write_text(
        """import os
import socket
from pathlib import Path

Path(os.environ["JARN_HELP_GUARD_MARKER"]).write_text("loaded", encoding="utf-8")

def denied(*_args, **_kwargs):
    raise AssertionError("network access attempted while rendering jarn --help")

class OfflineSocket(socket.socket):
    def connect(self, *_args, **_kwargs):
        denied()

    def connect_ex(self, *_args, **_kwargs):
        denied()

socket.socket = OfflineSocket
socket.create_connection = denied
socket.getaddrinfo = denied
""",
        encoding="utf-8",
    )
    jarn_home = tmp_path / "missing-jarn-home"
    env = dict(os.environ)
    env.update(
        {
            "COLUMNS": "80",
            "HOME": str(tmp_path / "missing-os-home"),
            "JARN_HELP_GUARD_MARKER": str(marker),
            "JARN_HOME": str(jarn_home),
            "NO_COLOR": "1",
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(guard_dir), env.get("PYTHONPATH", "")) if part
            ),
            "TERM": "dumb",
        }
    )

    completed = subprocess.run(  # noqa: S603 - fixed interpreter/module argv
        [sys.executable, "-m", "jarn", "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "loaded"
    assert not jarn_home.exists(), "--help must not create config or runtime state"
    assert completed.stderr == ""
    assert "\x1b[" not in completed.stdout
    assert "Traceback" not in completed.stdout
    help_text = completed.stdout
    for section in (
        "Start and common commands:",
        "Installation and configuration (no browser required):",
        "Authentication:",
        "Models and reasoning:",
        "Permissions and safety:",
        "Diagnosis, repair, and support:",
        "Update, rollback, and removal:",
    ):
        assert section in help_text
    for detail in (
        "resolved executable path",
        "install method/record",
        "setup state",
        "jarn auth login [--device]",
        "/model refresh",
        "reasoning efforts supported",
        "/mode plan|ask|auto-edit|yolo",
        "hard catastrophic-action and credential guards",
        "jarn doctor --fix --dry-run",
        "jarn doctor --report FILE",
        "jarn update --check",
        "jarn rollback",
        "jarn uninstall",
    ):
        assert detail in help_text
    assert len(help_text.splitlines()) <= 160
    assert len(help_text) <= 12_000


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
@pytest.mark.parametrize("arguments", [["doctor"], ["doctor", "--fix", "--dry-run"]])
def test_doctor_read_only_modes_do_not_repair_global_home_implicitly(
    tmp_path: Path, arguments: list[str]
) -> None:
    home = tmp_path / "jarn-home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    env = dict(os.environ)
    env.update({"JARN_HOME": str(home), "NO_COLOR": "1", "TERM": "dumb"})

    completed = subprocess.run(  # noqa: S603 - fixed interpreter/module argv
        [sys.executable, "-m", "jarn", *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0  # no config is itself a diagnostic finding
    assert stat.S_IMODE(home.stat().st_mode) == 0o755


def test_config_path_and_show_json_are_scoped_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    secret = "sk-super-secret-value-123456789"
    global_path = _write_current_config(home, secret=secret)
    monkeypatch.setenv("JARN_HOME", str(home))

    assert cli.main(["config", "show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["path"] == str(global_path)
    assert shown["config"]["providers"]["openai"]["api_key"] == "[REDACTED]"
    provenance = shown["provenance"]
    assert provenance["displayedValues"]["default_model"] == "global_config"
    assert provenance["displayedValues"]["providers.openai.api_key"] == "global_config"
    assert {layer["source"] for layer in provenance["runtimeLayers"]} == {
        "built_in_default",
        "global_config",
        "project_config",
        "environment",
        "cli_flag",
        "managed_policy",
    }
    assert secret not in json.dumps(shown)

    project = tmp_path / "project"
    (project / ".jarn").mkdir(parents=True)
    monkeypatch.chdir(project)
    assert cli.main(["config", "path", "--project", "--json"]) == 0
    scoped = json.loads(capsys.readouterr().out)
    assert scoped["scope"] == "project"
    assert scoped["path"] == str(project / ".jarn" / "config.yaml")


@pytest.mark.parametrize("content", [None, "providers: [unfinished"])
def test_config_validate_failure_is_non_mutating_and_has_full_error_anatomy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    monkeypatch.setenv("JARN_HOME", str(home))

    assert cli.main(["config", "validate", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["status"] in {"missing", "corrupt"}
    assert {
        "code",
        "summary",
        "cause",
        "component",
        "retryable",
        "action",
        "log_path",
        "log_available",
    } <= payload["error"].keys()
    assert (path.read_bytes() if path.exists() else None) == before


def test_config_validate_redacts_malformed_custom_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    credential = "arbitrary-provider-key-7q2"
    path = home / "config.yaml"
    path.write_text(
        "config_version: 3\nproviders:\n  leaky:\n"
        f"    type: openrouter\n    api_key: [{credential}]\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    monkeypatch.setenv("JARN_HOME", str(home))

    assert cli.main(["config", "validate", "--json"]) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["error"]["code"] == "JARN-CONFIG-002"
    assert credential not in output
    assert "api_key" in payload["error"]["cause"]
    assert path.read_bytes() == before


def test_config_validate_rejects_well_typed_plaintext_credential_without_echo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    credential = "opaque-provider-credential-7q2"
    path = home / "config.yaml"
    path.write_text(
        "config_version: 3\nproviders:\n  leaky:\n"
        f"    type: openrouter\n    api_key: {credential}\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    monkeypatch.setenv("JARN_HOME", str(home))

    assert cli.main(["config", "validate", "--json"]) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["ok"] is False
    assert payload["status"] == "invalid"
    assert payload["error"]["code"] == "JARN-CONFIG-002"
    assert "providers.leaky.api_key" in payload["error"]["cause"]
    assert credential not in output
    assert path.read_bytes() == before


def test_config_reset_is_confirmed_backed_up_atomic_and_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    path = _write_current_config(home)
    original = path.read_bytes()
    path.write_text(path.read_text(encoding="utf-8") + "\nunknown_extension: keep-before-reset\n")
    reset_source = path.read_bytes()
    monkeypatch.setenv("JARN_HOME", str(home))

    assert cli.main(["config", "reset", "--yes", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["preview"]["target"] == str(path)
    assert payload["preview"]["backup"] is True
    assert payload["preview"]["replacedCategories"]
    assert "sessions and transcripts" in payload["preview"]["preservedCategories"]
    backup = Path(payload["backup"])
    assert backup.read_bytes() == reset_source
    loaded = YAML().load(path.read_text(encoding="utf-8"))
    assert loaded["config_version"] == 3
    assert "unknown_extension" not in loaded
    assert path.read_bytes() == original
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("response", ["n", EOFError(), KeyboardInterrupt()])
def test_config_reset_preview_and_cancellation_are_actionable_and_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: str | BaseException,
) -> None:
    home = tmp_path / "home"
    path = _write_current_config(home)
    path.write_text(
        path.read_text(encoding="utf-8") + "\ncustom_extension: preserve-me\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    monkeypatch.setenv("JARN_HOME", str(home))

    def answer(_prompt: str) -> str:
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr("builtins.input", answer)
    assert cli.main(["config", "reset"]) == 130
    output = capsys.readouterr()
    assert "Configuration reset preview:" in output.out
    assert "custom or unknown top-level settings (1)" in output.out
    assert "Preserve: sessions and transcripts" in output.out
    assert "JARN-CLI-002" in output.err
    assert all(label in output.err for label in ("Cause:", "Component:", "Next:", "Log:"))
    assert path.read_bytes() == before
    assert not list(home.glob("config.yaml.bak*"))


def test_config_edit_validates_before_commit_and_refuses_concurrent_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    path = _write_current_config(home)
    original = path.read_bytes()
    monkeypatch.setenv("JARN_HOME", str(home))

    def valid_editor(argv: list[str], *, check: bool):
        assert check is False
        temporary = Path(argv[-1])
        text = temporary.read_text(encoding="utf-8")
        temporary.write_text(text.replace("telemetry: false", "telemetry: true"), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", valid_editor)
    assert cli.main(["config", "edit", "--editor", "fake-editor"]) == 0
    assert (home / "config.yaml.bak").read_bytes() == original
    assert YAML().load(path.read_text(encoding="utf-8"))["observability"]["telemetry"] is True
    capsys.readouterr()

    reviewed = path.read_bytes()

    def racing_editor(argv: list[str], *, check: bool):
        assert check is False
        temporary = Path(argv[-1])
        temporary.write_text(
            temporary.read_text(encoding="utf-8").replace("telemetry: true", "telemetry: false"),
            encoding="utf-8",
        )
        path.write_bytes(reviewed + b"\n# concurrent owner edit\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", racing_editor)
    assert cli.main(["config", "edit", "--editor", "fake-editor"]) == 2
    output = capsys.readouterr()
    assert "JARN-CONFIG-005" in output.err
    assert "Cause:" in output.err and "Next:" in output.err and "Log:" in output.err
    assert path.read_bytes() == reviewed + b"\n# concurrent owner edit\n"


def test_telemetry_on_off_and_status_are_explicit_and_do_not_create_install_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    path = _write_current_config(home)
    original = path.read_bytes()
    monkeypatch.setenv("JARN_HOME", str(home))

    assert cli.main(["telemetry", "on", "--json"]) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["enabled"] is True and enabled["changed"] is True
    assert Path(enabled["backup"]).read_bytes() == original
    assert not (home / ".install_id").exists()
    assert not (home / "telemetry.jsonl").exists()

    assert cli.main(["telemetry", "status", "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["enabled"] is True
    assert status_payload["valid_event_count"] == 0
    assert status_payload["health"] == "healthy"

    assert cli.main(["telemetry", "off", "--json"]) == 0
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["enabled"] is False and disabled["changed"] is True
    assert not (home / ".install_id").exists()


def test_telemetry_status_corruption_is_structured_nonzero_not_false_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_current_config(home)
    sink = home / "telemetry.jsonl"
    sink.write_bytes(b'{"event":"turn","ts":1,"install":"test"}\n{bad-json\n')
    monkeypatch.setenv("JARN_HOME", str(home))

    assert cli.main(["telemetry", "status", "--json"]) == EXIT_INTERNAL
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["health"] == "corrupt"
    assert payload["corruption_detected"] is True
    assert payload["valid_event_count"] == 1
    assert payload["error"]["code"] == "JARN-TELEMETRY-001"
    assert payload["error"]["component"] == "local telemetry sink"
    assert payload["error"]["retryable"] is True
    assert "telemetry off" in payload["error"]["action"]
    assert all(
        payload["error"].get(field)
        for field in ("summary", "cause", "component", "action", "log_path")
    )

    assert cli.main(["telemetry", "status"]) == EXIT_INTERNAL
    rendered = capsys.readouterr()
    assert "health: corrupt" in rendered.out
    assert "JARN-TELEMETRY-001" in rendered.err
    assert all(field in rendered.err for field in ("Cause:", "Component:", "Next:", "Log:"))


def test_telemetry_status_unreadable_sink_is_degraded_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_current_config(home)
    sink = home / "telemetry.jsonl"
    sink.write_text('{"event":"turn","ts":1,"install":"test"}\n', encoding="utf-8")
    monkeypatch.setenv("JARN_HOME", str(home))
    original_read_bytes = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path == sink:
            raise PermissionError("injected unreadable telemetry sink")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    assert cli.main(["telemetry", "status", "--json"]) == EXIT_INTERNAL
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["health"] == "degraded"
    assert payload["corruption_detected"] is False
    assert payload["error"]["code"] == "JARN-TELEMETRY-001"
    assert "unreadable telemetry sink" in payload["error"]["cause"]
    assert "permissions" in payload["error"]["action"]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs Windows privileges")
def test_telemetry_status_refuses_symlink_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _write_current_config(home)
    outside = tmp_path / "outside.jsonl"
    outside_bytes = b'{"event":"turn","ts":1,"install":"outside"}\n'
    outside.write_bytes(outside_bytes)
    (home / "telemetry.jsonl").symlink_to(outside)
    monkeypatch.setenv("JARN_HOME", str(home))

    assert cli.main(["telemetry", "status", "--json"]) == EXIT_INTERNAL
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["health"] == "degraded"
    assert payload["valid_event_count"] == 0
    assert payload["error"]["code"] == "JARN-TELEMETRY-001"
    assert "symbolic-link" in payload["error"]["cause"]
    assert outside.read_bytes() == outside_bytes


def test_telemetry_status_recovered_is_success_with_distinct_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jarn.observability.telemetry import Telemetry

    home = tmp_path / "home"
    _write_current_config(home)
    monkeypatch.setenv("JARN_HOME", str(home))

    def recovered_summary(_telemetry: Telemetry) -> dict[str, object]:
        return {
            "enabled": True,
            "path": str(home / "telemetry.jsonl"),
            "size_bytes": 42,
            "event_count": 1,
            "valid_event_count": 1,
            "corrupt_record_count": 0,
            "corrupt_record_lines": [],
            "corruption_detected": False,
            "repairable_final_record": False,
            "recovery_performed": True,
            "recovery_message": "removed malformed final telemetry record at line 2",
            "last_error": "",
            "health": "recovered",
            "install_id_present": False,
        }

    monkeypatch.setattr(Telemetry, "status_summary", recovered_summary)

    assert cli.main(["telemetry", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "error" not in payload
    assert payload["warning"]["code"] == "JARN-TELEMETRY-002"
    assert "removed malformed final telemetry record" in payload["warning"]["detail"]

    assert cli.main(["telemetry", "status"]) == 0
    rendered = capsys.readouterr()
    assert "health: recovered" in rendered.out
    assert "JARN-TELEMETRY-002" in rendered.err
    assert "JARN-TELEMETRY-001" not in rendered.err


def test_doctor_ga_options_delegate_to_shared_service_and_keep_legacy_json_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jarn.doctor.repair import RepairPlan, RepairResult
    from jarn.doctor.service import DoctorServiceResult

    seen: dict[str, object] = {}
    report = tmp_path / "support.json"

    def fake_service(**kwargs):
        seen.update(kwargs)
        return DoctorServiceResult(
            exit_code=0,
            diagnostics={"git": {"autocheckpoint": True}, "ok": True},
            repair_plan=RepairPlan(()),
            repair_result=RepairResult(True, (), ("manual-only",), True),
            report_path=report,
        )

    monkeypatch.setattr("jarn.doctor.service.run_doctor_service", fake_service)
    assert (
        cli.main(
            [
                "doctor",
                "--fix",
                "--dry-run",
                "--network",
                "--report",
                str(report),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["git"]["autocheckpoint"] is True
    assert payload["repair_result"]["dry_run"] is True
    assert payload["report_path"] == str(report)
    assert seen["fix"] is True and seen["dry_run"] is True and seen["network"] is True
    assert seen["report_path"] == report


def test_update_and_rollback_use_stable_exit_class_and_single_json_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def failed_update(**kwargs):
        seen.update(kwargs)
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "ok": False,
                    "changed": False,
                    "error": {"message": "ownership refused"},
                    "preview": {
                        "currentVersion": "1.0.0",
                        "targetVersion": "2.0.0",
                        "ownership": {"kind": "pipx"},
                        "config": {"migrationSteps": ["schema 2 -> 3"]},
                    },
                }
            )
        )
        return 1

    monkeypatch.setattr("jarn.update.run_update", failed_update)
    assert cli.main(["update", "--check", "--channel", "beta", "--json"]) == 7
    update_payload = json.loads(capsys.readouterr().out)
    assert update_payload["error"]["code"] == "JARN-UPDATE-001"
    assert update_payload["changed"] is False
    assert update_payload["preview"]["ownership"]["kind"] == "pipx"
    assert update_payload["preview"]["config"]["migrationSteps"] == ["schema 2 -> 3"]
    assert seen == {
        "channel": "beta",
        "check_only": True,
        "as_json": True,
        "dry_run": False,
        "version": None,
    }

    monkeypatch.setattr(
        "jarn.update.run_rollback",
        lambda **_kwargs: (
            print(json.dumps({"ok": False, "error": {"message": "no retained version"}})) or 1
        ),
    )
    assert cli.main(["rollback", "--json"]) == 7
    rollback_payload = json.loads(capsys.readouterr().out)
    assert rollback_payload["error"]["code"] == "JARN-UPDATE-001"
    assert rollback_payload["error"]["component"] == "updater"


def test_update_conflict_is_usage_error_and_uninstall_forwards_categories(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["update", "--check", "--dry-run", "--json"]) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["error"]["code"] == "JARN-CLI-001"

    seen: dict[str, object] = {}

    def fake_uninstall(*, yes: bool, categories: set[str] | None):
        seen.update(yes=yes, categories=categories)
        return 0

    monkeypatch.setattr("jarn.uninstall.run_uninstall", fake_uninstall)
    assert cli.main(["uninstall", "--yes", "--config", "--sessions"]) == 0
    assert seen == {"yes": True, "categories": {"config", "sessions"}}
