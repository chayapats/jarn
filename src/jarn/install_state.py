"""Read the installer-owned, versioned installation record.

The curl installer writes this record only after activating and smoke-testing the
user-visible command.  Runtime commands such as ``jarn update``, ``rollback``,
``doctor`` and ``uninstall`` use the same source of truth instead of guessing an
installation method from ``sys.frozen``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarn.util.atomic import atomic_write_text, file_lock

SETUP_STATUS_VALUES = frozenset({"required", "skipped", "in_progress", "incomplete", "complete"})
_ROLLBACK_NAME_RE = re.compile(r"^\.jarn\.rollback\.[0-9A-Za-z._-]+$")


class InstallStateError(RuntimeError):
    """The installation record is absent, malformed, or unsafe to act on."""


def default_state_dir() -> Path:
    """Return the state directory shared with ``install.sh``."""
    override = os.environ.get("JARN_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "jarn"


def default_manifest_path() -> Path:
    return default_state_dir() / "install.json"


@dataclass(frozen=True)
class InstallRecord:
    schema_version: int
    version: str
    method: str
    channel: str
    active_path: Path
    candidate_path: Path | None = None
    previous_path: Path | None = None
    state_dir: Path | None = None
    platform: dict[str, Any] = field(default_factory=dict)
    dependency: dict[str, Any] = field(default_factory=dict)
    activation: dict[str, Any] = field(default_factory=dict)
    setup_status: str = "unknown"
    installed_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> InstallRecord:
        if value.get("schema_version") != 1:
            raise InstallStateError(
                "JARN-UPDATE-001: unsupported install-record schema; "
                "run the current curl installer to repair it"
            )
        version = value.get("version")
        method = value.get("method")
        channel = value.get("channel")
        active_raw = value.get("active_path")
        if not (
            isinstance(version, str)
            and version
            and isinstance(method, str)
            and method
            and isinstance(channel, str)
            and channel
            and isinstance(active_raw, str)
            and active_raw
        ):
            raise InstallStateError(
                "JARN-UPDATE-002: install record is missing required fields; "
                "run `jarn doctor --report` and reinstall with the official curl command"
            )

        active = _absolute_path(active_raw, field_name="active_path")
        candidate = _optional_path(value.get("candidate_path"), field_name="candidate_path")
        previous = _optional_path(value.get("previous_path"), field_name="previous_path")
        state = _optional_path(value.get("state_dir"), field_name="state_dir")
        return cls(
            schema_version=1,
            version=version,
            method=method,
            channel=channel,
            active_path=active,
            candidate_path=candidate,
            previous_path=previous,
            state_dir=state,
            platform=_mapping(value.get("platform")),
            dependency=_mapping(value.get("dependency")),
            activation=_mapping(value.get("activation")),
            setup_status=str(value.get("setup_status") or "unknown"),
            installed_at=str(value.get("installed_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "method": self.method,
            "channel": self.channel,
            "active_path": str(self.active_path),
            "candidate_path": str(self.candidate_path) if self.candidate_path else None,
            "previous_path": str(self.previous_path) if self.previous_path else None,
            "state_dir": str(self.state_dir) if self.state_dir else None,
            "platform": self.platform,
            "dependency": self.dependency,
            "activation": self.activation,
            "setup_status": self.setup_status,
            "installed_at": self.installed_at,
        }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _absolute_path(value: str, *, field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallStateError(
            f"JARN-UPDATE-003: unsafe relative {field_name} in install record; "
            "run the official curl installer to regenerate it"
        )
    # Never permit a broad filesystem directory to become the target of a later
    # replace/unlink operation, even if the record was manually tampered with.
    if path == Path(path.anchor):
        raise InstallStateError(f"JARN-UPDATE-003: unsafe {field_name} in install record")
    if ".." in path.parts:
        raise InstallStateError(
            f"JARN-UPDATE-003: unsafe parent traversal in {field_name}; "
            "run the official curl installer to regenerate the record"
        )
    return Path(os.path.abspath(path))


def _optional_path(value: Any, *, field_name: str) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InstallStateError(f"JARN-UPDATE-002: invalid {field_name} in install record")
    return _absolute_path(value, field_name=field_name)


def load_install_record(path: Path | None = None) -> InstallRecord:
    """Load and validate an installer record, without following action targets."""
    manifest = path or default_manifest_path()
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InstallStateError(
            "JARN-UPDATE-004: no managed install record was found; "
            "repair the installation with the official curl installer"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallStateError(
            f"JARN-UPDATE-005: cannot read install record {manifest}: {exc}; "
            "run `jarn doctor --report` before reinstalling"
        ) from exc
    if not isinstance(raw, dict):
        raise InstallStateError("JARN-UPDATE-002: install record must be a JSON object")
    return InstallRecord.from_dict(raw)


def _standard_active_path(state_dir: Path) -> Path:
    """Derive the normal ``<prefix>/bin/jarn`` path from installer state."""
    if state_dir.name == "jarn" and state_dir.parent.name == "state":
        prefix = state_dir.parent.parent
    else:
        prefix = state_dir.parent
    return prefix / "bin" / "jarn"


def _custom_active_path_has_marker(record: InstallRecord) -> bool:
    """Accept a custom install directory only with the installer's local marker."""
    marker = record.active_path.parent / ".jarn-install-method"
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        fields = marker.read_text(encoding="utf-8").strip().split()
    except OSError:
        return False
    return (
        len(fields) >= 2
        and fields[0] in {"binary", "python", "existing"}
        and (fields[1] == record.version)
    )


def validate_install_record_actions(
    record: InstallRecord,
    *,
    manifest_path: Path | None = None,
) -> InstallRecord:
    """Fail closed before a receipt path is executed, replaced, or removed.

    JSON shape validation alone is not proof of ownership.  This binds state to
    the actual receipt location, limits the active command to the installer
    layout (or a custom directory carrying its installer marker), and constrains
    rollback names to the installer's private namespace.
    """
    manifest = Path(manifest_path or default_manifest_path()).expanduser()
    if not manifest.is_absolute():
        manifest = manifest.absolute()
    manifest = Path(os.path.abspath(manifest))
    if manifest.name != "install.json":
        raise InstallStateError(
            "JARN-UPDATE-003: unsafe install-record filename; expected install.json"
        )
    if manifest.is_symlink():
        raise InstallStateError(
            "JARN-UPDATE-003: refusing action through a symbolic-link install record"
        )
    if record.state_dir is None or record.state_dir != manifest.parent:
        raise InstallStateError(
            "JARN-UPDATE-003: install state directory does not match the receipt location; "
            "run the official curl installer to repair it"
        )
    if record.state_dir.is_symlink() or record.active_path.parent.is_symlink():
        raise InstallStateError(
            "JARN-UPDATE-003: refusing action through a symbolic-link managed directory"
        )
    if record.active_path.name != "jarn":
        raise InstallStateError("JARN-UPDATE-003: managed active path must end in /jarn")

    expected = _standard_active_path(record.state_dir)
    override = os.environ.get("JARN_INSTALL_DIR")
    allowed = {expected}
    if override:
        install_dir = Path(override).expanduser()
        if not install_dir.is_absolute():
            install_dir = install_dir.absolute()
        allowed.add(Path(os.path.abspath(install_dir)) / "jarn")
    if record.active_path not in allowed and not _custom_active_path_has_marker(record):
        raise InstallStateError(
            "JARN-UPDATE-003: active path is outside the proven installer-owned layout; "
            "set JARN_INSTALL_DIR to the exact custom directory or rerun the official installer"
        )
    if record.active_path.exists() and record.active_path.is_dir():
        raise InstallStateError(
            "JARN-UPDATE-003: active executable path unexpectedly names a directory"
        )

    previous = record.previous_path
    if previous is not None:
        if previous.parent != record.active_path.parent or not _ROLLBACK_NAME_RE.fullmatch(
            previous.name
        ):
            raise InstallStateError(
                "JARN-UPDATE-003: retained rollback path is outside the managed namespace"
            )
        if previous.exists() and previous.is_dir():
            raise InstallStateError(
                "JARN-UPDATE-003: rollback executable path unexpectedly names a directory"
            )
    return record


def load_actionable_install_record(path: Path | None = None) -> InstallRecord:
    """Load a receipt and prove every path that later code may mutate."""
    manifest = path or default_manifest_path()
    return validate_install_record_actions(load_install_record(manifest), manifest_path=manifest)


def update_setup_status(
    status: str,
    *,
    path: Path | None = None,
    updated_at: str | None = None,
) -> bool:
    """Atomically update installer-owned setup status without rewriting guesses.

    Returns ``False`` when no managed install record exists (development, pip,
    or another unmanaged install).  A present but malformed/unsafe record fails
    closed so onboarding cannot claim that installer state is synchronized.
    """

    if status not in SETUP_STATUS_VALUES:
        raise ValueError(f"invalid setup status: {status!r}")
    target = path or default_manifest_path()
    if not target.exists():
        return False
    if target.is_symlink():
        raise InstallStateError(
            "JARN-UPDATE-003: refusing to update setup status through a symbolic link"
        )
    with file_lock(target) as locked:
        if not locked:
            raise InstallStateError(
                "JARN-UPDATE-005: could not lock the install record; setup status was not changed"
            )
        record = load_actionable_install_record(target)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallStateError(
                f"JARN-UPDATE-005: cannot re-read install record {target}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise InstallStateError("JARN-UPDATE-002: install record must be a JSON object")
        # ``record`` above validates all action-bearing paths before publication.
        raw["setup_status"] = status
        raw["setup_updated_at"] = updated_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        atomic_write_text(target, text, mode=0o600)
        installed = load_actionable_install_record(target)
        if installed.setup_status != status or installed.active_path != record.active_path:
            raise InstallStateError(
                "JARN-UPDATE-005: install-record setup status verification failed"
            )
    return True


__all__ = [
    "SETUP_STATUS_VALUES",
    "InstallRecord",
    "InstallStateError",
    "default_manifest_path",
    "default_state_dir",
    "load_actionable_install_record",
    "load_install_record",
    "update_setup_status",
    "validate_install_record_actions",
]
