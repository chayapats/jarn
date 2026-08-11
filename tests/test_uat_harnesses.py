"""Safety and evidence contracts for the manual GA UAT harnesses."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
UAT_DIR = ROOT / "scripts" / "uat"
HARNESSES = sorted(UAT_DIR.glob("uat-*.sh"))
CURRENT_VERSION = re.search(
    r'^version\s*=\s*"([^"]+)"\s*$',
    (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    re.MULTILINE,
).group(1)


def test_all_six_goal_uats_have_reproducible_harnesses() -> None:
    assert [path.name for path in HARNESSES] == [
        "uat-001-ubuntu-ssh.sh",
        "uat-002-legacy-collision.sh",
        "uat-003-macos-desktop.sh",
        "uat-004-anthropic.sh",
        "uat-005-ollama.sh",
        "uat-006-network-failure.sh",
    ]


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda path: path.stem)
def test_default_mode_is_local_dry_run_and_never_invokes_ssh(
    tmp_path: Path, harness: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "ssh-invoked"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f"#!/bin/sh\n: > {str(marker)!r}\nexit 99\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(harness), "--host", "nobody@example.invalid"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "no SSH connection" in result.stdout
    assert not marker.exists()


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda path: path.stem)
def test_dry_run_can_write_explicit_not_run_evidence(
    tmp_path: Path, harness: Path
) -> None:
    uat_number = harness.name.split("-", 2)[1]
    output = tmp_path / f"uat-{uat_number}.json"
    result = subprocess.run(
        [
            "bash",
            str(harness),
            "--host",
            "nobody@example.invalid",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    record = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads((UAT_DIR / "result.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(record, schema)
    assert record["uat_id"] == f"UAT-{uat_number}"
    assert record["schema_version"] == 2
    assert record["candidate_version"] == CURRENT_VERSION
    assert record["status"] == "not_run"
    assert "no target state changed" in record["result"].lower()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_result_writer_redacts_host_home_and_secret_shapes(tmp_path: Path) -> None:
    output = tmp_path / "redacted.json"
    result = subprocess.run(
        [
            "python3",
            str(UAT_DIR / "write_result.py"),
            "--output",
            str(output),
            "--uat-id",
            "UAT-001",
            "--candidate-version",
            "9.8.7",
            "--candidate-commit",
            "a" * 40,
            "--status",
            "failed",
            "--started-at",
            "2026-08-09T00:00:00Z",
            "--ended-at",
            "2026-08-09T00:00:01Z",
            "--command",
            "ssh alice@host /home/alice/bin/jarn token=secret-value-123",
            "--result",
            "device_code=ABCD-EFGH password hunter2",
            "--redact-host",
            "alice@host",
            "--redact-home",
            "/home/alice",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    serialized = output.read_text(encoding="utf-8")
    assert "alice@host" not in serialized
    assert "/home/alice" not in serialized
    assert "secret-value-123" not in serialized
    assert "ABCD-EFGH" not in serialized
    assert "hunter2" not in serialized
    assert "[HOST]" in serialized and "$HOME" in serialized
    record = json.loads(serialized)
    assert record["candidate_version"] == "9.8.7"
    assert record["candidate_commit"] == "a" * 40


def test_result_writer_supports_non_uat_release_gate_evidence(tmp_path: Path) -> None:
    output = tmp_path / "release-gate.json"
    result = subprocess.run(
        [
            "python3",
            str(UAT_DIR / "write_result.py"),
            "--output",
            str(output),
            "--record-id",
            "REVIEW-GA-001",
            "--criterion-id",
            "GATE-RELEASE",
            "--criterion-id",
            "COMPLETION-W",
            "--status",
            "blocked",
            "--started-at",
            "2026-08-09T00:00:00Z",
            "--ended-at",
            "2026-08-09T00:00:01Z",
            "--command",
            "manual release review",
            "--result",
            "not all underlying evidence has passed",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    record = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads((UAT_DIR / "result.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(record, schema)
    assert record["record_id"] == "REVIEW-GA-001"
    assert record["candidate_version"] == CURRENT_VERSION
    assert "uat_id" not in record
    assert record["criterion_ids"] == ["GATE-RELEASE", "COMPLETION-W"]


@pytest.mark.parametrize("harness", HARNESSES, ids=lambda path: path.stem)
def test_execute_requires_a_host_before_any_ssh_call(
    tmp_path: Path, harness: Path
) -> None:
    marker = tmp_path / "ssh-invoked"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f"#!/bin/sh\n: > {str(marker)!r}\nexit 99\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(harness), "--execute"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "--host USER@HOST is required" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    "endpoint",
    ["http://192.168.1.20:11434", "http://localhost:70000", "https://localhost:11434"],
)
def test_ollama_rejects_unsafe_endpoint_before_ssh(
    tmp_path: Path, endpoint: str
) -> None:
    marker = tmp_path / "ssh-invoked"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        f"#!/bin/sh\n: > {str(marker)!r}\nexit 99\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        [
            "bash",
            str(UAT_DIR / "uat-005-ollama.sh"),
            "--host",
            "nobody@example.invalid",
            "--endpoint",
            endpoint,
            "--execute",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "local endpoint" in result.stderr
    assert not marker.exists()


def test_anthropic_harness_never_accepts_api_key_argument() -> None:
    secret = "sk-ant-test-secret-value"
    result = subprocess.run(
        [
            "bash",
            str(UAT_DIR / "uat-004-anthropic.sh"),
            "--api-key",
            secret,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert secret not in result.stdout
    assert secret not in result.stderr


def _write_fixture_ssh(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
remote=""
for argument in "$@"; do remote=$argument; done
/bin/sh -n -c "$remote" || exit 90
count=0
[ ! -f "$JARN_FAKE_SSH_COUNT" ] || count=$(sed -n '1p' "$JARN_FAKE_SSH_COUNT")
count=$((count + 1))
printf '%s\\n' "$count" > "$JARN_FAKE_SSH_COUNT"
case "$JARN_FAKE_SSH_SCENARIO:$count" in
  uat1:1)
    printf '%s\n' 'os=ubuntu' 'version=22.04' 'arch=x86_64' 'libc=glibc 2.35'
    ;;
  uat1:2)
    printf '%s\n' 'home=/home/uat' 'resolution=/home/uat/.local/bin/jarn' \
      'config=present' 'node=/usr/bin/node' 'python=/usr/bin/python3' 'uv=absent'
    ;;
  macos:1)
    printf '%s\\n' 'os=macos' 'version=14.6' 'arch=arm64' 'libc=unknown'
    ;;
  macos:2)
    printf '%s\\n' 'home=/Users/uat' 'login_user=uat' 'console_user=uat' \\
      'resolution=/Users/uat/.local/bin/jarn' 'version=jarn 1.0.0' \\
      'config=absent' 'catalog=absent' 'auth=not_ready'
    ;;
  macos:3)
    printf '%s\n' 'https://auth.example.invalid/callback?device_code=DO-NOT-PERSIST'
    ;;
  macos:4)
    printf '%s\\n' 'auth=verified' 'doctor=valid' 'profile=verified' \\
      'route=verified' 'catalog_path=present' 'catalog=live_verified'
    ;;
  anthropic:1|ollama:1)
    printf '%s\\n' 'os=ubuntu' 'version=22.04' 'arch=x86_64' 'libc=glibc 2.35'
    ;;
  anthropic:2)
    printf '%s\\n' 'home=/home/uat' 'resolution=/home/uat/.local/bin/jarn' \\
      'version=jarn 1.0.0' 'config=absent' 'key=present'
    ;;
  anthropic:3) ;;
  anthropic:4)
    printf '%s\\n' 'config_present=yes' 'config_mode=600' 'config_ref=environment' \\
      'config_leak=no' 'logs_leak=no' 'doctor=valid' 'profile=verified' 'route=verified'
    ;;
  ollama:2)
    printf '%s\\n' 'home=/home/uat' 'resolution=/home/uat/.local/bin/jarn' \\
      'version=jarn 1.0.0' 'config=absent' 'timeout=present' \\
      'endpoint=reachable' 'catalog=nonempty'
    ;;
  ollama:3) ;;
  ollama:4)
    printf '%s\\n' 'config_present=yes' 'profile=verified' 'route=verified' \\
      'endpoint_config=verified' 'keyless=verified' 'doctor=valid' \\
      'endpoint=reachable' 'catalog=nonempty' 'local_turn=passed' 'local_rc=0' \\
      'missing_failure=nonzero' 'missing_rc=4' 'remediation=actionable' 'success_text=no'
    ;;
  *) exit 91 ;;
esac
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_uat001_blocked_preflight_redacts_remote_home(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fixture_ssh(fake_bin / "ssh")
    output = tmp_path / "uat001-blocked.json"
    env = os.environ.copy()
    env.update(
        {
            "JARN_FAKE_SSH_COUNT": str(tmp_path / "ssh-count"),
            "JARN_FAKE_SSH_SCENARIO": "uat1",
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )

    result = subprocess.run(
        [
            "bash",
            str(UAT_DIR / "uat-001-ubuntu-ssh.sh"),
            "--host",
            "uat@fixture",
            "--output",
            str(output),
            "--execute",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    serialized = output.read_text(encoding="utf-8")
    record = json.loads(serialized)
    assert record["status"] == "blocked"
    assert record["redaction"]["home_path_redacted"] is True
    assert "/home/uat" not in serialized
    assert "$HOME" in serialized
    assert (tmp_path / "ssh-count").read_text(encoding="utf-8").strip() == "2"


@pytest.mark.parametrize(
    ("scenario", "script", "manual_answers"),
    [
        ("macos", "uat-003-macos-desktop.sh", ["y"] * 9),
        ("anthropic", "uat-004-anthropic.sh", ["y"] * 6 + ["n"]),
        ("ollama", "uat-005-ollama.sh", ["y"] * 7),
    ],
)
def test_new_uat_execute_orchestration_can_pass_only_complete_fixture_evidence(
    tmp_path: Path,
    scenario: str,
    script: str,
    manual_answers: list[str],
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fixture_ssh(fake_bin / "ssh")
    output = tmp_path / f"{scenario}.json"
    env = os.environ.copy()
    env.update(
        {
            "JARN_FAKE_SSH_COUNT": str(tmp_path / "ssh-count"),
            "JARN_FAKE_SSH_SCENARIO": scenario,
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    stdin = "uat@fixture\n" + "\n".join(manual_answers) + "\n"

    result = subprocess.run(
        [
            "bash",
            str(UAT_DIR / script),
            "--host",
            "uat@fixture",
            "--output",
            str(output),
            "--execute",
        ],
        cwd=ROOT,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["status"] == "passed"
    assert record["errors"] == []
    assert record["redaction"]["raw_auth_output_persisted"] is False
    assert record["redaction"]["raw_terminal_output_persisted"] is False
    assert record["redaction"]["secrets_allowed"] is False
    assert "DO-NOT-PERSIST" not in output.read_text(encoding="utf-8")
    assert (tmp_path / "ssh-count").read_text(encoding="utf-8").strip() == "4"


@pytest.mark.parametrize(
    ("scenario", "script"),
    [
        ("macos", "uat-003-macos-desktop.sh"),
        ("anthropic", "uat-004-anthropic.sh"),
        ("ollama", "uat-005-ollama.sh"),
    ],
)
def test_new_uat_refuses_mutation_when_exact_host_confirmation_does_not_match(
    tmp_path: Path, scenario: str, script: str
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fixture_ssh(fake_bin / "ssh")
    output = tmp_path / f"{scenario}-declined.json"
    env = os.environ.copy()
    env.update(
        {
            "JARN_FAKE_SSH_COUNT": str(tmp_path / "ssh-count"),
            "JARN_FAKE_SSH_SCENARIO": scenario,
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )

    result = subprocess.run(
        [
            "bash",
            str(UAT_DIR / script),
            "--host",
            "uat@fixture",
            "--output",
            str(output),
            "--execute",
        ],
        cwd=ROOT,
        env=env,
        input="not-the-host\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["status"] == "not_run"
    assert "declined" in record["result"].lower()
    # Only the read-only platform and fixture probes ran; no interactive setup did.
    assert (tmp_path / "ssh-count").read_text(encoding="utf-8").strip() == "2"


def test_operator_observation_failure_cannot_fake_pass(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fixture_ssh(fake_bin / "ssh")
    output = tmp_path / "macos-failed.json"
    env = os.environ.copy()
    env.update(
        {
            "JARN_FAKE_SSH_COUNT": str(tmp_path / "ssh-count"),
            "JARN_FAKE_SSH_SCENARIO": "macos",
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    stdin = "uat@fixture\nn\n" + "y\n" * 8

    result = subprocess.run(
        [
            "bash",
            str(UAT_DIR / "uat-003-macos-desktop.sh"),
            "--host",
            "uat@fixture",
            "--output",
            str(output),
            "--execute",
        ],
        cwd=ROOT,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert any("default browser" in error for error in record["errors"])
