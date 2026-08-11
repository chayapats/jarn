"""GA error, doctor repair, and privacy-boundary acceptance tests."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from jarn.config.secrets import redact_structure
from jarn.doctor.repair import RepairAction, RepairPlan, apply_repair_plan, build_repair_plan
from jarn.doctor.report import (
    build_support_report,
    scan_support_report,
    support_report_json,
    write_support_report,
)
from jarn.errors import ErrorCode, JarnUserError, error_detail


def test_error_codes_are_unique_and_error_anatomy_is_complete_and_redacted():
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values))
    raw_secret = "sk-1234567890abcdefghijklmnop"

    detail = error_detail(
        ErrorCode.AUTH_FAILED,
        "Sign-in failed.",
        cause=f"provider returned Bearer {raw_secret}",
        component="authentication",
        retryable=True,
        action="Run jarn login and retry.",
        details={"refresh_token": "pin", "nested": {"value": raw_secret}},
    )
    data = detail.to_dict()

    assert set(data) == {
        "code",
        "summary",
        "cause",
        "component",
        "retryable",
        "action",
        "log_path",
        "log_available",
        "report_path",
        "details",
    }
    assert data["code"] == "JARN-AUTH-001"
    assert raw_secret not in json.dumps(data)
    assert data["details"]["refresh_token"] == "[REDACTED]"
    rendered = detail.render()
    assert "Cause:" in rendered
    assert "Component:" in rendered
    assert "retryable: yes" in rendered
    assert "Next:" in rendered
    assert "Log:" in rendered


def test_recursive_redaction_copies_input_and_redacts_short_sensitive_fields():
    source = {
        "password": "1234",
        "safe": ["hello", {"authorization": "x"}],
    }
    result = redact_structure(source)
    assert result == {
        "password": "[REDACTED]",
        "safe": ["hello", {"authorization": "[REDACTED]"}],
    }
    assert source["password"] == "1234"


def _support_diag(secret: str, local_path: str) -> dict:
    return {
        "ok": False,
        "jarn": {
            "version": "0.11.0",
            "active_executable": local_path,
            "path_candidates": [local_path, f"{local_path}-old"],
            "shadowed": [f"{local_path}-old"],
            "install": {"method": "uv-tool"},
        },
        "platform": {
            "system": "Linux",
            "release": "6.8",
            "architecture": "x86_64",
            "libc": {"name": "glibc", "version": "2.35"},
            "python": {"version": "3.12.4", "executable": local_path, "managed": True},
        },
        "shell": {
            "name": "bash",
            "profile": f"{local_path}/.bashrc",
            "path_value": local_path,
            "profile_present": True,
        },
        "installation": {"directory": local_path, "writable": True, "free_bytes": 20},
        "dependencies": {
            "uv": {"path": local_path, "ok": True, "version": "uv 1.0"},
            "codex": {
                "path": local_path,
                "ok": True,
                "version": "codex 1.0",
                "protocol": {"compatible": True},
            },
        },
        "configuration": {
            "schema_current": 3,
            "global": {
                "path": f"{local_path}/config.yaml",
                "status": "current",
                "source_version": 3,
                "target_version": 3,
            },
            "project": None,
        },
        "secrets": {
            "store_present": True,
            "file_count": 1,
            "root_mode": "0700",
            "permission_issues": [{"path": local_path, "mode": "0644"}],
        },
        "auth": {"providers": [{"name": "openrouter", "authenticated": False, "mode": secret}]},
        "catalog": {"source": "live", "freshness": "fresh", "cache_present": True},
        "selected_route": {"model": secret, "available": False, "error": local_path},
        "workspace": {"root": local_path, "trusted": True},
        "sandbox": {"backend": "bubblewrap", "available": True, "mode": "strict"},
        "network": {
            "checked": True,
            "checks": [
                {
                    "provider": "openrouter",
                    "host": secret,
                    "port": 443,
                    "reachable": True,
                    "timeout_seconds": 0.5,
                }
            ],
        },
        "update": {"channel": "stable", "checks_enabled": True},
        "extensions": {"counts": {"skills": 2}, "file_contents": secret},
        "prompt_modules": {"prompt": secret},
        "errors": [
            {
                "code": "JARN-AUTH-001",
                "summary": "Authentication failed.",
                "cause": f"{secret} at {local_path}",
                "component": "authentication",
                "retryable": False,
                "action": f"edit {local_path}",
                "log_path": local_path,
            }
        ],
        "warnings": [f"secret={secret} file={local_path}"],
    }


def test_support_report_is_strictly_allowlisted_path_free_and_secret_scanned(
    tmp_path: Path,
):
    secret = "plain-provider-secret-123"
    local_path = "/Users/alice/private-project"
    diag = _support_diag(secret, local_path)

    report = build_support_report(diag, known_secrets={secret})
    text = support_report_json(diag, known_secrets={secret})

    assert report["jarn"]["executable_candidates"] == 2
    assert report["provider_access"] == [{"provider": "openrouter", "authenticated": False}]
    assert secret not in text
    assert local_path not in text
    assert "prompt_modules" not in text
    assert "file_contents" not in text
    assert "cause" not in report["errors"][0]
    assert scan_support_report(text, known_secrets={secret}) == []

    destination = tmp_path / "support.json"
    assert write_support_report(diag, destination, known_secrets={secret}) == destination
    assert json.loads(destination.read_text(encoding="utf-8"))["report_version"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_support_scanner_detects_secrets_paths_and_forbidden_payload_fields():
    text = json.dumps(
        {
            "prompt": "sk-1234567890abcdefghijklmnop",
            "location": "/nix/store/private-project",
        }
    )
    findings = scan_support_report(text)
    assert "unredacted secret-shaped value" in findings
    assert "absolute POSIX path" in findings
    assert "forbidden field: prompt" in findings


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission repair")
def test_repair_plan_dry_run_then_apply_is_scoped_and_recoverable(tmp_path: Path):
    home = tmp_path / ".jarn"
    home.mkdir(mode=0o755)
    secret = home / "secrets" / "jarn" / "openrouter"
    secret.parent.mkdir(parents=True, mode=0o755)
    secret.write_text("reference-only", encoding="utf-8")
    secret.chmod(0o644)
    config = home / "config.yaml"
    original = "# old\nui:\n  theme: light\n"
    config.write_text(original, encoding="utf-8")
    inventory = {
        "secrets": {
            "permission_issues": [{"path": str(secret), "mode": "0644"}],
        },
        "configuration": {
            "global": {
                "path": str(config),
                "status": "migration-required",
                "source_version": 0,
                "target_version": 3,
            },
            "project": None,
        },
    }

    plan = build_repair_plan(inventory, global_home=home)
    preview = apply_repair_plan(plan, global_home=home, dry_run=True)
    assert preview.ok is True
    assert stat.S_IMODE(home.stat().st_mode) == 0o755
    assert stat.S_IMODE(secret.stat().st_mode) == 0o644
    assert config.read_text(encoding="utf-8") == original

    applied = apply_repair_plan(plan, global_home=home, dry_run=False)
    assert applied.ok is True
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert "config_version: 3" in config.read_text(encoding="utf-8")
    assert list(home.glob("config.yaml.bak.*"))


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior differs on Windows")
def test_repair_planner_refuses_symlink_secret_target(tmp_path: Path):
    home = tmp_path / ".jarn"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do not chmod", encoding="utf-8")
    target = home / "secrets" / "link"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    before = stat.S_IMODE(outside.stat().st_mode)

    plan = build_repair_plan(
        {
            "secrets": {
                "permission_issues": [{"path": str(target), "mode": "0777"}],
            },
            "configuration": {},
        },
        global_home=home,
    )

    assert all(action.target != target for action in plan.actions)
    assert plan.skipped
    assert stat.S_IMODE(outside.stat().st_mode) == before


def test_repair_executor_revalidates_forged_config_scope(tmp_path: Path):
    home = tmp_path / ".jarn"
    home.mkdir()
    outside = tmp_path / "outside.yaml"
    source = "ui:\n  theme: light\n"
    outside.write_text(source, encoding="utf-8")
    forged = RepairPlan((
        RepairAction(
            id="forged",
            kind="config-migration",
            description="forged target",
            target=outside,
            before="0",
            after="3",
            scope_root=home,
        ),
    ))

    result = apply_repair_plan(forged, global_home=home, dry_run=False)

    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == "JARN-DOCTOR-002"
    assert outside.read_text(encoding="utf-8") == source


def test_repair_executor_refuses_permission_widening_and_rolls_back_batch(
    tmp_path: Path,
):
    home = tmp_path / ".jarn"
    home.mkdir(mode=0o700)
    config = home / "config.yaml"
    source = "# original\nui:\n  theme: light\n"
    config.write_text(source, encoding="utf-8")
    plan = RepairPlan((
        RepairAction(
            id="config.migrate.global",
            kind="config-migration",
            description="valid migration",
            target=config,
            before="0",
            after="3",
            scope_root=home,
        ),
        RepairAction(
            id="forged-widening",
            kind="chmod",
            description="must be refused",
            target=home,
            before="0700",
            after="0777",
            scope_root=home,
        ),
    ))

    result = apply_repair_plan(plan, global_home=home, dry_run=False)

    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == "JARN-DOCTOR-002"
    assert config.read_text(encoding="utf-8") == source
    if os.name != "nt":
        assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_report_writer_refuses_symlink(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "report.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(JarnUserError) as caught:
        write_support_report({}, link)

    assert caught.value.code == "JARN-DOCTOR-003"
    assert target.read_text(encoding="utf-8") == "keep"
