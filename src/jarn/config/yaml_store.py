"""Shared round-trip YAML store helpers: fail-closed load + backup + atomic write.

Used by the global config editor (:class:`jarn.config.settings.ConfigStore`) and
the project permission-rule store
(:class:`jarn.permissions.rule_store.PermissionRuleStore`). Both edit a YAML file
in place and must never silently overwrite a corrupt file with a near-empty dict
— so :func:`load_yaml_doc` raises :class:`ConfigCorruptError` (after saving a
``.bak``) instead of returning ``{}``, and :func:`atomic_write_yaml` rotates a
backup of the current file before every overwrite. A *missing* file is distinct
from a *corrupt* one: missing → ``{}`` (legitimate bootstrap), corrupt → raise.

Backups: ``<path>.bak`` (most recent) and ``<path>.bak.1`` (previous), so the
last two good writes (or the corrupt file itself, when corruption is detected at
load) are always recoverable.
"""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class ConfigCorruptError(ValueError):
    """The persisted YAML file is unreadable (corrupt or I/O error).

    The file is left untouched and a ``.bak`` copy has been saved so the user can
    repair it. Distinct from a missing file, which is a legitimate bootstrap.
    """


def _yaml() -> YAML:
    y = YAML()  # round-trip mode: preserves comments + style
    y.preserve_quotes = True
    return y


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def rotate_backup(path: Path) -> Path | None:
    """Copy the current file to ``<path>.bak``, demoting the old ``.bak`` to ``.bak.1``.

    Keeps the last two backups. Returns the backup path, or ``None`` when the
    file does not exist (nothing to back up).
    """
    if not path.is_file():
        return None
    bak = backup_path(path)
    bak1 = path.with_name(path.name + ".bak.1")
    if bak.exists():
        os.replace(bak, bak1)  # promote the previous backup before overwriting it
    shutil.copy2(path, bak)
    return bak


def _corrupt(path: Path, detail: str) -> ConfigCorruptError:
    bak = rotate_backup(path)
    saved = f"; a backup was saved at {bak!s}" if bak is not None else ""
    return ConfigCorruptError(
        f"{detail} The file was NOT modified{saved}. "
        "Run `jarn doctor` or fix the YAML by hand."
    )


def load_yaml_doc(path: Path) -> dict:
    """Load a YAML file as a dict, fail-closed on corruption.

    Missing file → ``{}`` (legitimate bootstrap). An empty file (parses to
    ``None``) → ``{}``. A YAML/OS error or a non-mapping top level →
    :class:`ConfigCorruptError` (the file is backed up first and left untouched).
    """
    if not path.is_file():
        return {}
    try:
        loaded = _yaml().load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise _corrupt(path, f"Could not read {path!s}: {exc}.") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise _corrupt(
            path,
            f"{path!s} top-level must be a mapping (got {type(loaded).__name__}).",
        )
    return loaded


def atomic_write_yaml(path: Path, data: dict) -> None:
    """Back up the current file, then atomically write *data* (comment-preserving)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_backup(path)
    buf = io.StringIO()
    _yaml().dump(data, buf)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, path)
