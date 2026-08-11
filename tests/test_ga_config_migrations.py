"""GA acceptance coverage for transactional on-disk config migrations."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest
from ruamel.yaml import YAML

import jarn.config.migrations as migrations
from jarn.config.loader import ConfigError, load_config
from jarn.config.migrations import (
    apply_config_migration,
    diagnose_config_file,
    migrate_config_file,
    plan_config_migration,
)
from jarn.config.pydantic_schema import CURRENT_CONFIG_VERSION
from jarn.config.pydantic_schema import migrate_config as migrate_config_mapping
from jarn.config.trust import project_dangerous
from jarn.errors import JarnUserError

_V0_CUSTOM = """\
# keep this operator comment
default_profile: custom
default_model: custom/acme-model
permission_mode: auto-edit
log_level: debug
providers:
  custom:
    type: openai_compatible
    base_url: https://models.example.invalid/v1
    api_key: ${ACME_API_KEY}
    headers:
      X-Tenant: blue
    timeout: 17
routing:
  main: custom/acme-model
permissions:
  allow:
    - git status
ui:
  theme: light
  accent: magenta
"""


def _load_yaml(path: Path) -> dict:
    loaded = YAML().load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_migration_preserves_customization_comments_backup_and_mode(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(_V0_CUSTOM, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o640)

    result = migrate_config_file(path)

    assert result.applied is True
    assert result.backup_path is not None
    assert result.backup_path.name.startswith("config.yaml.bak.")
    assert result.backup_path.read_text(encoding="utf-8") == _V0_CUSTOM
    migrated_text = path.read_text(encoding="utf-8")
    assert "# keep this operator comment" in migrated_text
    migrated = _load_yaml(path)
    assert migrated["config_version"] == CURRENT_CONFIG_VERSION
    assert migrated["providers"]["custom"]["timeout"] == 17
    assert migrated["providers"]["custom"]["headers"]["X-Tenant"] == "blue"
    assert migrated["routing"]["main"] == "custom/acme-model"
    assert migrated["permissions"]["allow"] == ["git status"]
    assert migrated["ui"]["theme"] == "light"
    assert "log_level" not in migrated
    assert migrated["observability"]["log_level"] == "debug"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        assert stat.S_IMODE(result.backup_path.stat().st_mode) == 0o640


def test_dry_run_validates_without_writing_or_backing_up(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(_V0_CUSTOM, encoding="utf-8")

    result = migrate_config_file(path, dry_run=True)

    assert result.changed is True
    assert result.applied is False
    assert result.dry_run is True
    assert path.read_text(encoding="utf-8") == _V0_CUSTOM
    assert not list(tmp_path.glob("config.yaml.bak.*"))


@pytest.mark.parametrize(
    ("text", "status", "code"),
    [
        ("providers: [\n", "corrupt", "JARN-CONFIG-001"),
        ("config_version: 999\n", "unsupported", "JARN-CONFIG-003"),
        ("config_version: nope\n", "invalid", "JARN-CONFIG-002"),
    ],
)
def test_corrupt_invalid_and_future_configs_are_actionable_and_unchanged(
    tmp_path: Path,
    text: str,
    status: str,
    code: str,
):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    diagnostic = diagnose_config_file(path)

    assert diagnostic.status == status
    assert diagnostic.error is not None
    assert diagnostic.error["code"] == code
    assert diagnostic.recovery_actions
    assert path.read_text(encoding="utf-8") == text
    assert not list(tmp_path.glob("config.yaml.bak.*"))


def test_corruption_diagnostic_lists_recovery_backup_location(tmp_path: Path):
    path = tmp_path / "config.yaml"
    backup = tmp_path / "config.yaml.bak.20260809T000000Z"
    path.write_text("broken: [\n", encoding="utf-8")
    backup.write_text("config_version: 3\n", encoding="utf-8")

    diagnostic = diagnose_config_file(path)

    assert diagnostic.status == "corrupt"
    assert diagnostic.backup_paths == (backup,)
    assert str(backup) in diagnostic.recovery_actions[1]


def test_stale_plan_refuses_concurrent_overwrite(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(_V0_CUSTOM, encoding="utf-8")
    plan = plan_config_migration(path)
    newer = _V0_CUSTOM.replace("accent: magenta", "accent: cyan")
    path.write_text(newer, encoding="utf-8")

    with pytest.raises(JarnUserError) as caught:
        apply_config_migration(plan)

    assert caught.value.code == "JARN-CONFIG-004"
    assert path.read_text(encoding="utf-8") == newer
    assert not list(tmp_path.glob("config.yaml.bak.*"))


def test_migration_refuses_to_write_without_exclusive_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    path = tmp_path / "config.yaml"
    path.write_text(_V0_CUSTOM, encoding="utf-8")

    @contextmanager
    def unavailable_lock(_path: Path):
        yield False

    monkeypatch.setattr(migrations, "file_lock", unavailable_lock)

    with pytest.raises(JarnUserError) as caught:
        migrate_config_file(path)

    assert caught.value.code == "JARN-CONFIG-005"
    assert path.read_text(encoding="utf-8") == _V0_CUSTOM
    assert not list(tmp_path.glob("config.yaml.bak.*"))


def test_post_activation_failure_rolls_back_exact_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    path = tmp_path / "config.yaml"
    path.write_text(_V0_CUSTOM, encoding="utf-8")

    def interrupted(target: Path, text: str, *, mode: int | None) -> None:
        target.write_text(text, encoding="utf-8")
        raise OSError("simulated activation interruption")

    monkeypatch.setattr(migrations, "_publish_validated", interrupted)

    with pytest.raises(JarnUserError) as caught:
        migrate_config_file(path)

    assert caught.value.code == "JARN-CONFIG-004"
    assert path.read_text(encoding="utf-8") == _V0_CUSTOM
    backups = list(tmp_path.glob("config.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == _V0_CUSTOM


def test_rollback_failure_is_never_mislabeled_as_recovered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    path = tmp_path / "config.yaml"
    path.write_text(_V0_CUSTOM, encoding="utf-8")

    def interrupted(target: Path, text: str, *, mode: int | None) -> None:
        target.write_text(text, encoding="utf-8")
        raise OSError("activation failed")

    def rollback_failed(target: Path, backup: Path, *, mode: int | None) -> None:
        raise OSError("rollback failed")

    monkeypatch.setattr(migrations, "_publish_validated", interrupted)
    monkeypatch.setattr(migrations, "_restore_backup", rollback_failed)

    with pytest.raises(JarnUserError) as caught:
        migrate_config_file(path)

    assert "both failed" in caught.value.detail.summary
    assert "manually restore" in caught.value.detail.action


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior differs on Windows")
def test_symlink_migration_is_refused(tmp_path: Path):
    real = tmp_path / "real.yaml"
    link = tmp_path / "config.yaml"
    real.write_text(_V0_CUSTOM, encoding="utf-8")
    link.symlink_to(real)

    with pytest.raises(JarnUserError) as caught:
        migrate_config_file(link)

    assert caught.value.code == "JARN-CONFIG-005"
    assert real.read_text(encoding="utf-8") == _V0_CUSTOM


def test_current_config_is_an_idempotent_noop(tmp_path: Path):
    path = tmp_path / "config.yaml"
    source = f"config_version: {CURRENT_CONFIG_VERSION}\nui:\n  theme: dark\n"
    path.write_text(source, encoding="utf-8")

    first = migrate_config_file(path)
    second = migrate_config_file(path)

    assert first.changed is second.changed is False
    assert path.read_text(encoding="utf-8") == source
    assert not list(tmp_path.glob("config.yaml.bak.*"))


@pytest.mark.parametrize(
    "source",
    [
        "ui:\n  theme: light\n",
        "config_version: 1\npolicy:\n  profile: offline\n  web_tools: true\n",
        "config_version: 2\nexecution:\n  multimodal: false\n  backend: local\n",
    ],
)
def test_actual_global_load_migrates_every_supported_prior_schema(
    tmp_path: Path,
    source: str,
):
    path = tmp_path / "global.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.warns(UserWarning) if "profile:" in source or "multimodal:" in source else nullcontext():
        config = load_config(global_path=path, project_path=None, project_root=tmp_path)

    assert config.ui.theme in {"dark", "light"}
    assert _load_yaml(path)["config_version"] == CURRENT_CONFIG_VERSION
    backups = list(tmp_path.glob("global.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == source


def test_trusted_project_load_migrates_disk_and_preserves_trust_semantics(
    tmp_path: Path,
):
    global_path = tmp_path / "global.yaml"
    project_path = tmp_path / ".jarn" / "config.yaml"
    project_path.parent.mkdir()
    global_path.write_text("config_version: 3\n", encoding="utf-8")
    source = "config_version: 2\nexecution:\n  multimodal: false\n  backend: local\n"
    project_path.write_text(source, encoding="utf-8")
    project_raw = _load_yaml(project_path)

    with pytest.warns(UserWarning):
        config = load_config(
            global_path=global_path,
            project_path=project_path,
            project_root=tmp_path,
            project_trusted=True,
            project_raw=project_raw,
        )

    assert config.execution.backend == "local"
    assert _load_yaml(project_path)["config_version"] == CURRENT_CONFIG_VERSION
    assert list(project_path.parent.glob("config.yaml.bak.*"))[0].read_text(
        encoding="utf-8"
    ) == source


def test_trusted_empty_project_config_is_still_migrated(tmp_path: Path):
    global_path = tmp_path / "global.yaml"
    project_path = tmp_path / ".jarn" / "config.yaml"
    project_path.parent.mkdir()
    global_path.write_text("config_version: 3\n", encoding="utf-8")
    project_path.write_text("", encoding="utf-8")

    load_config(
        global_path=global_path,
        project_path=project_path,
        project_root=tmp_path,
        project_trusted=True,
        project_raw={},
    )

    assert _load_yaml(project_path)["config_version"] == CURRENT_CONFIG_VERSION
    backups = list(project_path.parent.glob("config.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == ""


def test_project_trust_fingerprint_uses_effective_migrated_schema():
    prior = {
        "config_version": 2,
        "execution": {"multimodal": False, "backend": "local"},
        "routing": {"main": "openrouter/example"},
    }
    with pytest.warns(UserWarning):
        migrated = migrate_config_mapping(prior)

    assert project_dangerous(prior) == project_dangerous(migrated)


def test_untrusted_project_load_does_not_mutate_repository_config(tmp_path: Path):
    global_path = tmp_path / "global.yaml"
    project_path = tmp_path / ".jarn" / "config.yaml"
    project_path.parent.mkdir()
    global_path.write_text("config_version: 3\n", encoding="utf-8")
    source = "config_version: 2\nexecution:\n  multimodal: false\n  backend: local\n"
    project_path.write_text(source, encoding="utf-8")

    load_config(
        global_path=global_path,
        project_path=project_path,
        project_root=tmp_path,
        project_trusted=False,
    )

    assert project_path.read_text(encoding="utf-8") == source
    assert not list(project_path.parent.glob("config.yaml.bak.*"))


def test_project_raw_migration_refuses_changed_disk(tmp_path: Path):
    global_path = tmp_path / "global.yaml"
    project_path = tmp_path / ".jarn" / "config.yaml"
    project_path.parent.mkdir()
    global_path.write_text("config_version: 3\n", encoding="utf-8")
    project_path.write_text("ui:\n  theme: light\n", encoding="utf-8")
    trusted_raw = _load_yaml(project_path)
    changed = "ui:\n  theme: dark\n"
    project_path.write_text(changed, encoding="utf-8")

    with pytest.raises(ConfigError, match="changed after its trust read"):
        load_config(
            global_path=global_path,
            project_path=project_path,
            project_root=tmp_path,
            project_trusted=True,
            project_raw=trusted_raw,
        )

    assert project_path.read_text(encoding="utf-8") == changed
    assert not list(project_path.parent.glob("config.yaml.bak.*"))


def test_corrupt_global_load_reports_stable_recovery_error_without_overwrite(
    tmp_path: Path,
):
    path = tmp_path / "global.yaml"
    source = "providers: [\n"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ConfigError, match="JARN-CONFIG-001"):
        load_config(global_path=path, project_path=None, project_root=tmp_path)

    assert path.read_text(encoding="utf-8") == source
    assert not list(tmp_path.glob("global.yaml.bak.*"))
