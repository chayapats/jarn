"""Transactional, comment-preserving on-disk configuration migrations.

The schema migrators in :mod:`jarn.config.pydantic_schema` are pure transforms.
This module is the filesystem boundary: it plans without mutation, validates the
source and candidate, saves a timestamped byte-for-byte backup, publishes through
an adjacent temporary file, validates the installed result, and restores the
backup if post-publication verification ever fails.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from jarn.config.pydantic_schema import (
    CURRENT_CONFIG_VERSION,
    ConfigValidationError,
    migrate_config,
    parse_config_model,
    safe_config_validation_message,
)
from jarn.errors import ErrorCode, JarnUserError, config_error
from jarn.util.atomic import file_lock

ConfigStatus = Literal[
    "missing", "current", "migration-required", "corrupt", "invalid", "unsupported"
]


@dataclass(frozen=True, slots=True)
class ConfigFileDiagnostic:
    path: Path
    status: ConfigStatus
    source_version: int | None
    target_version: int
    message: str
    recovery_actions: tuple[str, ...] = ()
    error: dict[str, Any] | None = None
    backup_paths: tuple[Path, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in {"missing", "current", "migration-required"}


@dataclass(frozen=True, slots=True)
class ConfigMigrationPlan:
    path: Path
    source_version: int
    target_version: int
    source_sha256: str
    changed: bool
    summary: tuple[str, ...]
    candidate_text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ConfigMigrationResult:
    path: Path
    source_version: int
    target_version: int
    changed: bool
    applied: bool
    dry_run: bool
    backup_path: Path | None
    summary: tuple[str, ...]
    installed_sha256: str | None = None


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    return yaml


def _parse_document(text: str, path: Path) -> dict[str, Any]:
    try:
        loaded = _yaml().load(text)
    except YAMLError as exc:
        raise config_error(
            ErrorCode.CONFIG_INVALID_YAML,
            "Configuration YAML is damaged or incomplete.",
            cause=str(exc),
            action=(
                f"Keep {path} unchanged; restore a .bak file or run doctor repair "
                "after correcting the YAML."
            ),
            path=path,
        ) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise config_error(
            ErrorCode.CONFIG_INVALID_SCHEMA,
            "Configuration root must be a YAML mapping.",
            cause=f"The document root is {type(loaded).__name__}, not a mapping.",
            action=f"Edit {path} so its top level contains key/value settings.",
            path=path,
        )
    return loaded


def _version_of(raw: dict[str, Any], path: Path) -> int:
    version = raw.get("config_version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        raise config_error(
            ErrorCode.CONFIG_INVALID_SCHEMA,
            "Configuration version is invalid.",
            cause=f"config_version must be an integer, got {version!r}.",
            action=f"Restore a valid backup of {path} or set an integer config_version.",
            path=path,
        )
    if version > CURRENT_CONFIG_VERSION:
        raise config_error(
            ErrorCode.CONFIG_UNSUPPORTED_VERSION,
            "Configuration was written by a newer J.A.R.N. release.",
            cause=(f"File schema is {version}; this executable supports {CURRENT_CONFIG_VERSION}."),
            action="Upgrade J.A.R.N.; do not downgrade or rewrite this configuration.",
            path=path,
        )
    return version


def _render(raw: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml().dump(raw, buf)
    return buf.getvalue()


def _validate_candidate(raw: dict[str, Any], path: Path) -> None:
    try:
        parse_config_model(raw)
    except ConfigValidationError as exc:
        raise config_error(
            ErrorCode.CONFIG_INVALID_SCHEMA,
            "Configuration does not match the supported schema.",
            cause=safe_config_validation_message(exc, raw=raw),
            action=f"Correct {path}, restore a timestamped backup, or inspect doctor output.",
            path=path,
        ) from exc
    except Exception as exc:
        # Pydantic's ValidationError is intentionally kept out of this module's
        # public API; callers receive one stable J.A.R.N. error shape.
        raise config_error(
            ErrorCode.CONFIG_INVALID_SCHEMA,
            "Configuration does not match the supported schema.",
            cause=safe_config_validation_message(exc, raw=raw),
            action=f"Correct {path}, restore a timestamped backup, or inspect doctor output.",
            path=path,
        ) from exc


def plan_config_migration(path: Path) -> ConfigMigrationPlan:
    """Return a validated migration plan without changing *path*."""
    path = Path(path)
    if path.is_symlink():
        raise config_error(
            ErrorCode.CONFIG_WRITE_FAILED,
            "Refusing to migrate a configuration through a symbolic link.",
            cause="The target identity could change between validation and activation.",
            action="Replace the symlink with an explicitly managed regular config file.",
            path=path,
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise config_error(
            ErrorCode.CONFIG_INVALID_YAML,
            "Configuration could not be read.",
            cause=str(exc),
            action=f"Check ownership and read permission for {path}, then retry.",
            path=path,
            retryable=True,
        ) from exc
    raw = _parse_document(text, path)
    source_version = _version_of(raw, path)
    try:
        migrated = migrate_config(raw)
    except ConfigValidationError as exc:
        raise config_error(
            ErrorCode.CONFIG_UNSUPPORTED_VERSION,
            "No safe configuration migration path is available.",
            cause=str(exc),
            action="Upgrade J.A.R.N. or restore a configuration from a supported release.",
            path=path,
        ) from exc
    _validate_candidate(migrated, path)
    candidate = _render(migrated)
    changed = source_version != CURRENT_CONFIG_VERSION
    steps = tuple(
        f"schema {version} -> {version + 1}"
        for version in range(source_version, CURRENT_CONFIG_VERSION)
    )
    return ConfigMigrationPlan(
        path=path,
        source_version=source_version,
        target_version=CURRENT_CONFIG_VERSION,
        source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        changed=changed,
        summary=steps or ("schema already current",),
        candidate_text=candidate,
    )


def diagnose_config_file(path: Path) -> ConfigFileDiagnostic:
    """Inspect one config file and return actionable diagnostics, never mutate it."""
    path = Path(path)
    if not path.exists():
        return ConfigFileDiagnostic(
            path, "missing", None, CURRENT_CONFIG_VERSION, "configuration file is absent"
        )
    try:
        plan = plan_config_migration(path)
    except JarnUserError as exc:
        backups = tuple(
            sorted(
                path.parent.glob(f"{path.name}.bak.*"),
                key=lambda candidate: candidate.name,
                reverse=True,
            )
        )
        code = exc.code
        if code == ErrorCode.CONFIG_INVALID_YAML.value:
            status: ConfigStatus = "corrupt"
        elif code == ErrorCode.CONFIG_UNSUPPORTED_VERSION.value:
            status = "unsupported"
        else:
            status = "invalid"
        return ConfigFileDiagnostic(
            path=path,
            status=status,
            source_version=None,
            target_version=CURRENT_CONFIG_VERSION,
            message=exc.detail.summary,
            recovery_actions=(
                exc.detail.action,
                (
                    f"Newest recovery candidate: {backups[0]}"
                    if backups
                    else f"No timestamped backup was found beside {path}."
                ),
                "Review the file before applying any repair.",
            ),
            error=exc.to_dict(),
            backup_paths=backups,
        )
    status = "migration-required" if plan.changed else "current"
    message = (
        f"migration required ({plan.source_version} -> {plan.target_version})"
        if plan.changed
        else f"schema {plan.target_version} is current"
    )
    return ConfigFileDiagnostic(
        path,
        status,
        plan.source_version,
        plan.target_version,
        message,
        ("Run doctor repair to create a timestamped backup and migrate.",) if plan.changed else (),
    )


def _timestamped_backup(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = path.with_name(f"{path.name}.bak.{stamp}")
    # O_EXCL gives a hard no-clobber guarantee even under same-microsecond tests.
    fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as out, path.open("rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            candidate.unlink()
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    with contextlib.suppress(OSError):
        shutil.copystat(path, candidate)
    _fsync_directory(path.parent)
    return candidate


def _fsync_directory(directory: Path) -> None:
    """Best-effort persistence of a directory-entry change."""
    dir_fd: int | None = None
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _publish_validated(path: Path, text: str, *, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.migrate-", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            tmp.chmod(mode)
        candidate_raw = _parse_document(tmp.read_text(encoding="utf-8"), path)
        _validate_candidate(candidate_raw, path)
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _restore_backup(path: Path, backup: Path, *, mode: int | None) -> None:
    """Restore *backup* byte-for-byte through a durable adjacent replacement."""
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.rollback-", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as out, backup.open("rb") as source:
            shutil.copyfileobj(source, out)
            out.flush()
            os.fsync(out.fileno())
        if mode is not None:
            tmp.chmod(mode)
        # Refuse to activate a backup that is no longer valid, even during
        # recovery.  Old schema versions are accepted because validation passes
        # through the same pure migration chain without altering these bytes.
        raw = _parse_document(tmp.read_text(encoding="utf-8"), path)
        _version_of(raw, path)
        _validate_candidate(raw, path)
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def apply_config_migration(plan: ConfigMigrationPlan) -> ConfigMigrationResult:
    """Apply a previously planned migration with TOCTOU and rollback guards."""
    if not plan.changed:
        return ConfigMigrationResult(
            plan.path,
            plan.source_version,
            plan.target_version,
            False,
            False,
            False,
            None,
            plan.summary,
            plan.source_sha256,
        )
    path = plan.path
    with file_lock(path) as locked:
        if not locked:
            raise config_error(
                ErrorCode.CONFIG_WRITE_FAILED,
                "Configuration migration could not acquire its write lock.",
                cause="The config directory does not support a safe exclusive lock.",
                action="Check directory ownership/filesystem locking support and retry.",
                path=path,
                retryable=True,
            )
        try:
            current_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise config_error(
                ErrorCode.CONFIG_WRITE_FAILED,
                "Configuration migration could not re-read its source.",
                cause=str(exc),
                action="Check file ownership and retry; the source was not changed.",
                path=path,
                retryable=True,
            ) from exc
        current_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
        if current_hash != plan.source_sha256:
            raise config_error(
                ErrorCode.CONFIG_MIGRATION_FAILED,
                "Configuration changed after migration was planned.",
                cause="A concurrent process or editor modified the file.",
                action="Review the newer file and generate a fresh migration plan.",
                path=path,
                retryable=True,
            )
        mode = None
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(path.stat().st_mode)
        backup: Path | None = None
        try:
            backup = _timestamped_backup(path)
            _publish_validated(path, plan.candidate_text, mode=mode)
            # Verify the installed bytes through the same schema boundary.
            installed = _parse_document(path.read_text(encoding="utf-8"), path)
            _validate_candidate(installed, path)
            if _version_of(installed, path) != plan.target_version:
                raise ValueError("installed config version does not match migration target")
        except Exception as exc:
            # A failure before os.replace leaves the original in place.  A failure
            # after replace is restored from the immutable backup atomically.
            rollback_error: Exception | None = None
            if backup is not None and backup.is_file():
                try:
                    _restore_backup(path, backup, mode=mode)
                    if path.read_bytes() != backup.read_bytes():
                        raise OSError("restored config does not match its backup")
                except Exception as restore_exc:  # noqa: BLE001 - integrity boundary
                    rollback_error = restore_exc
            if rollback_error is not None:
                summary = "Configuration migration and automatic rollback both failed."
                cause = f"migration: {exc}; rollback: {rollback_error}"
                action = (
                    f"Stop using the config and manually restore the byte backup {backup}."
                )
            elif backup is not None:
                summary = "Configuration migration failed and the prior file was restored."
                cause = str(exc)
                action = f"Inspect {backup}; fix the reported problem and retry the migration."
            else:
                summary = "Configuration migration failed before activation."
                cause = str(exc)
                action = "Check directory permissions and free space, then retry."
            raise config_error(
                ErrorCode.CONFIG_MIGRATION_FAILED,
                summary,
                cause=cause,
                action=action,
                path=path,
                retryable=True,
            ) from exc
    return ConfigMigrationResult(
        path,
        plan.source_version,
        plan.target_version,
        True,
        True,
        False,
        backup,
        plan.summary,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def restore_config_backup(
    path: Path,
    backup_path: Path,
    *,
    expected_sha256: str | None = None,
) -> None:
    """Safely restore one migration-owned backup.

    The optional expected digest prevents a doctor batch rollback from erasing a
    user edit made after migration activation.
    """
    path = Path(path)
    backup = Path(backup_path)
    if (
        path.is_symlink()
        or backup.is_symlink()
        or not backup.is_file()
        or backup.parent != path.parent
        or not backup.name.startswith(f"{path.name}.bak.")
    ):
        raise config_error(
            ErrorCode.CONFIG_WRITE_FAILED,
            "Configuration backup restore target is unsafe.",
            cause="The target or backup identity did not match the migration backup contract.",
            action="Restore the backup manually only after verifying both paths.",
            path=path,
        )
    backup_text = backup.read_text(encoding="utf-8")
    backup_raw = _parse_document(backup_text, path)
    _version_of(backup_raw, path)
    _validate_candidate(backup_raw, path)
    with file_lock(path) as locked:
        if not locked:
            raise config_error(
                ErrorCode.CONFIG_WRITE_FAILED,
                "Configuration backup restore could not acquire its write lock.",
                cause="The config directory does not support a safe exclusive lock.",
                action=f"Keep {backup} and retry after checking filesystem permissions.",
                path=path,
                retryable=True,
            )
        if expected_sha256 is not None:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if current != expected_sha256:
                raise config_error(
                    ErrorCode.CONFIG_MIGRATION_FAILED,
                    "Configuration changed before batch rollback.",
                    cause="Refusing to overwrite a newer user or process edit.",
                    action=f"Compare {path} with {backup} and choose which version to keep.",
                    path=path,
                )
        mode = None
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(path.stat().st_mode)
        _restore_backup(path, backup, mode=mode)
        if path.read_bytes() != backup.read_bytes():
            raise config_error(
                ErrorCode.CONFIG_MIGRATION_FAILED,
                "Configuration backup restore verification failed.",
                cause="Activated bytes differ from the selected backup.",
                action=f"Stop using the config and manually restore {backup}.",
                path=path,
            )


def migrate_config_file(path: Path, *, dry_run: bool = False) -> ConfigMigrationResult:
    """Plan and optionally apply one config migration.

    ``dry_run=True`` performs the complete parse/transform/schema validation and
    reports the exact steps, but creates no backup and changes no bytes.
    """
    plan = plan_config_migration(path)
    if dry_run:
        return ConfigMigrationResult(
            plan.path,
            plan.source_version,
            plan.target_version,
            plan.changed,
            False,
            True,
            None,
            plan.summary,
            None,
        )
    return apply_config_migration(plan)


__all__ = [
    "ConfigFileDiagnostic",
    "ConfigMigrationPlan",
    "ConfigMigrationResult",
    "apply_config_migration",
    "diagnose_config_file",
    "migrate_config_file",
    "plan_config_migration",
    "restore_config_backup",
]
