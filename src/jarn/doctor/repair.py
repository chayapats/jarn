"""Safe, scoped, recoverable doctor repair plans.

Planning never mutates.  Applying revalidates every target and rolls back all
earlier changes if a later action fails.  Only owner-permission tightening and
validated config migrations are supported; arbitrary commands and package
installation intentionally do not belong in ``doctor --fix``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jarn.config.migrations import migrate_config_file, restore_config_backup
from jarn.errors import ErrorCode, JarnUserError, error_detail

RepairKind = Literal["chmod", "config-migration"]


@dataclass(frozen=True, slots=True)
class RepairAction:
    id: str
    kind: RepairKind
    description: str
    target: Path
    before: str
    after: str
    recoverable: bool = True
    scope_root: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "target": str(self.target),
            "before": self.before,
            "after": self.after,
            "recoverable": self.recoverable,
            "scope_root": str(self.scope_root) if self.scope_root else None,
        }


@dataclass(frozen=True, slots=True)
class RepairPlan:
    actions: tuple[RepairAction, ...]
    skipped: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "skipped": list(self.skipped),
        }


@dataclass(frozen=True, slots=True)
class RepairResult:
    dry_run: bool
    applied: tuple[dict[str, Any], ...]
    skipped: tuple[str, ...]
    ok: bool
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "applied": list(self.applied),
            "skipped": list(self.skipped),
            "ok": self.ok,
            "error": self.error,
        }


def _mode(path: Path) -> int | None:
    if os.name == "nt":
        return None
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def build_repair_plan(
    inventory: dict[str, Any],
    *,
    global_home: Path,
) -> RepairPlan:
    """Derive the allowlisted repair plan from doctor inventory."""
    actions: list[RepairAction] = []
    skipped: list[str] = []
    home = Path(global_home)
    home_mode = _mode(home)
    if home.is_symlink():
        skipped.append("global home permission repair refused: target is a symlink")
    elif home_mode is not None and home_mode & 0o077:
        actions.append(
            RepairAction(
                "permissions.global-home",
                "chmod",
                "Restrict the J.A.R.N. state directory to its owner.",
                home,
                f"{home_mode:04o}",
                "0700",
                scope_root=home,
            )
        )

    secret_diag = inventory.get("secrets") or {}
    for index, issue in enumerate(secret_diag.get("permission_issues") or []):
        target = Path(str(issue.get("path", "")))
        try:
            resolved = target.resolve()
            safe_root = (home / "secrets").resolve()
            safe = resolved == safe_root or safe_root in resolved.parents
        except (OSError, RuntimeError):
            safe = False
        if not safe or target.is_symlink():
            skipped.append(f"unsafe secret permission target refused: {target}")
            continue
        desired = "0700" if target.is_dir() else "0600"
        actions.append(
            RepairAction(
                f"permissions.secret.{index}",
                "chmod",
                "Restrict a J.A.R.N. secret-store target to its owner.",
                target,
                str(issue.get("mode") or "unknown"),
                desired,
                scope_root=home,
            )
        )

    config_diag = inventory.get("configuration") or {}
    for tier in ("global", "project"):
        entry = config_diag.get(tier)
        if not entry:
            continue
        status = entry.get("status")
        target = Path(str(entry.get("path", "")))
        if status == "migration-required":
            scope_root: Path | None
            expected: Path | None
            if tier == "global":
                scope_root = home
                expected = home / "config.yaml"
            else:
                workspace_raw = (inventory.get("workspace") or {}).get("root")
                scope_root = Path(str(workspace_raw)) if workspace_raw else None
                expected = scope_root / ".jarn" / "config.yaml" if scope_root else None
            try:
                scoped = (
                    scope_root is not None
                    and expected is not None
                    and target.resolve() == expected.resolve()
                )
            except (OSError, RuntimeError):
                scoped = False
            if target.is_symlink() or target.parent.is_symlink():
                skipped.append(
                    f"{tier} config migration refused: target or parent is a symlink"
                )
                continue
            if not scoped or not target.is_file():
                skipped.append(f"{tier} config migration refused: target is outside its scope")
                continue
            actions.append(
                RepairAction(
                    f"config.migrate.{tier}",
                    "config-migration",
                    f"Back up, validate, and migrate the {tier} configuration.",
                    target,
                    str(entry.get("source_version")),
                    str(entry.get("target_version")),
                    scope_root=scope_root,
                )
            )
        elif status in {"corrupt", "invalid", "unsupported"}:
            # Doctor never guesses how to rewrite corrupt or future config.
            skipped.append(
                f"{tier} config requires manual recovery ({status}); no automatic edit planned"
            )
    return RepairPlan(tuple(actions), tuple(skipped))


def _unsafe_repair(reason: str, target: Path) -> JarnUserError:
    return JarnUserError(
        error_detail(
            ErrorCode.DOCTOR_REPAIR_UNSAFE,
            "Doctor refused an unsafe repair target.",
            cause=reason,
            component="doctor repair",
            retryable=False,
            action="Collect a fresh doctor repair plan; do not edit the target automatically.",
            details={"target": str(target)},
        )
    )


def _validate_chmod_action(action: RepairAction, home: Path) -> None:
    target = action.target
    if target.is_symlink():
        raise _unsafe_repair("symbolic-link chmod targets are refused", target)
    try:
        resolved = target.resolve()
        safe_home = home.resolve()
        secret_root = (home / "secrets").resolve()
    except (OSError, RuntimeError) as exc:
        raise _unsafe_repair(f"could not resolve repair scope: {exc}", target) from exc
    if resolved == safe_home:
        desired = "0700"
    elif resolved == secret_root or secret_root in resolved.parents:
        desired = "0700" if target.is_dir() else "0600"
    else:
        raise _unsafe_repair("permission target escaped the allowlisted secret store", target)
    if action.after != desired or action.scope_root != home:
        raise _unsafe_repair("permission mode or declared scope was not allowlisted", target)


def _validate_config_target(action: RepairAction) -> None:
    target = action.target
    scope = action.scope_root
    if target.is_symlink() or target.parent.is_symlink():
        reason = "configuration target or parent is a symbolic link"
    elif scope is None:
        reason = "configuration repair action has no declared scope"
    else:
        global_expected = scope / "config.yaml"
        project_expected = scope / ".jarn" / "config.yaml"
        try:
            matches = target.resolve() in {
                global_expected.resolve(),
                project_expected.resolve(),
            }
        except (OSError, RuntimeError):
            matches = False
        reason = "configuration target escaped its declared scope" if not matches else ""
    if reason:
        raise _unsafe_repair(reason, target)


def apply_repair_plan(
    plan: RepairPlan,
    *,
    global_home: Path,
    dry_run: bool = True,
) -> RepairResult:
    """Preview or apply a repair plan; rollback the batch on any failure."""
    if dry_run:
        return RepairResult(
            True,
            tuple({**action.to_dict(), "status": "planned"} for action in plan.actions),
            plan.skipped,
            True,
        )

    applied: list[dict[str, Any]] = []
    rollback: list[tuple[str, Path, Any]] = []
    home = Path(global_home)
    try:
        for action in plan.actions:
            if action.kind == "chmod":
                _validate_chmod_action(action, home)
                previous = _mode(action.target)
                if previous is None:
                    raise OSError(f"could not read permissions of {action.target}")
                action.target.chmod(int(action.after, 8))
                rollback.append(("chmod", action.target, previous))
                applied.append({**action.to_dict(), "status": "applied"})
                continue

            _validate_config_target(action)
            result = migrate_config_file(action.target, dry_run=False)
            if result.backup_path is not None:
                rollback.append(
                    (
                        "config",
                        action.target,
                        (result.backup_path, result.installed_sha256),
                    )
                )
            applied.append(
                {
                    **action.to_dict(),
                    "status": "applied" if result.applied else "unchanged",
                    "backup": str(result.backup_path) if result.backup_path else None,
                }
            )
    except Exception as exc:  # noqa: BLE001 - rollback boundary
        rollback_failures: list[str] = []
        for kind, target, value in reversed(rollback):
            if kind == "chmod":
                try:
                    target.chmod(int(value))
                except OSError:
                    rollback_failures.append("permission rollback failed")
            elif isinstance(value, tuple) and isinstance(value[0], Path):
                try:
                    restore_config_backup(
                        target,
                        value[0],
                        expected_sha256=value[1],
                    )
                except Exception:  # noqa: BLE001 - report integrity, never suppress it
                    rollback_failures.append("configuration rollback failed")
        if rollback_failures:
            detail = error_detail(
                ErrorCode.DOCTOR_CHECK_FAILED,
                "Doctor repair failed and one or more rollback steps also failed.",
                cause=f"{exc}; {'; '.join(rollback_failures)}",
                component="doctor repair",
                retryable=False,
                action="Stop and inspect the listed config backups before retrying repair.",
            )
        elif isinstance(exc, JarnUserError):
            detail = exc.detail
        else:
            detail = error_detail(
                ErrorCode.DOCTOR_CHECK_FAILED,
                "Doctor could not apply the safe repair plan.",
                cause=str(exc),
                component="doctor repair",
                retryable=True,
                action="Review the failed action; prior actions were rolled back.",
            )
        return RepairResult(False, tuple(applied), plan.skipped, False, detail.to_dict())
    return RepairResult(False, tuple(applied), plan.skipped, True)


__all__ = [
    "RepairAction",
    "RepairPlan",
    "RepairResult",
    "apply_repair_plan",
    "build_repair_plan",
]
