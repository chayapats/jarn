"""Transactional update and explicit rollback commands.

Updates deliberately delegate installation to the same reviewed ``install.sh``
used by the public one-liner.  That keeps platform selection, checksums, staging,
activation verification, and automatic rollback in one implementation.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version

from jarn.config.migrations import diagnose_config_file, plan_config_migration
from jarn.config.paths import global_config_path
from jarn.config.pydantic_schema import CURRENT_CONFIG_VERSION
from jarn.config.secrets import redact_secrets
from jarn.install_state import (
    InstallRecord,
    InstallStateError,
    default_manifest_path,
    load_actionable_install_record,
)
from jarn.util.atomic import atomic_write_text, file_lock
from jarn.util.process_env import external_command_env
from jarn.version import __version__

_RELEASES_API = "https://api.github.com/repos/chayapats/jarn/releases?per_page=30"
_RAW_BASE = "https://raw.githubusercontent.com/chayapats/jarn"
_RELEASE_DOWNLOAD_BASE = "https://github.com/chayapats/jarn/releases/download"
_NETWORK_TIMEOUT = 10.0
_MAX_RELEASE_CATALOG_BYTES = 512 * 1024
_MAX_RELEASE_NOTES_CHARS = 4_000
_MAX_RELEASE_TITLE_CHARS = 200
_MAX_BREAKING_ITEMS = 12
_MAX_BREAKING_ITEM_CHARS = 300
_CANARY_MODE_ENV = "JARN_UPDATE_CANARY_MODE"
_CANARY_SOURCE_ENVS = {
    "releases_api": "JARN_UPDATE_CANARY_RELEASES_API",
    "raw_base": "JARN_UPDATE_CANARY_RAW_BASE",
    "download_base": "JARN_UPDATE_CANARY_DOWNLOAD_BASE",
}


class UpdateError(RuntimeError):
    """An update or rollback could not be safely completed."""


def _independent_executable_env() -> dict[str, str]:
    """Start managed executables as independent frozen applications.

    A PyInstaller one-file process records its archive by path.  Update and
    rollback intentionally replace the executable at that same path, so a
    post-activation smoke child must not reuse the running parent's extraction
    directory for the now-different archive.  PyInstaller's public reset flag
    makes the child unpack and own a fresh runtime; non-frozen commands ignore
    it.
    """
    return external_command_env()


@dataclass(frozen=True)
class InstallOwnership:
    """Bounded, user-visible ownership decision for the active command."""

    kind: str
    method: str
    source: str
    active_path: Path | None
    updater_managed: bool
    installer_method: str | None = None
    manager_command: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "method": self.method,
            "source": self.source,
            "activePath": str(self.active_path) if self.active_path else None,
            "updaterManaged": self.updater_managed,
            "installerMethod": self.installer_method,
            "managerCommand": " ".join(self.manager_command) if self.manager_command else None,
        }


@dataclass(frozen=True)
class ReleasePreview:
    version: str
    title: str
    url: str
    published_at: str | None
    notes: str
    notes_truncated: bool
    breaking_changes: tuple[str, ...]
    breaking_changes_status: str
    config_schema_version: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "title": self.title,
            "url": self.url,
            "publishedAt": self.published_at,
            "notes": self.notes,
            "notesTruncated": self.notes_truncated,
            "breakingChanges": list(self.breaking_changes),
            "breakingChangesStatus": self.breaking_changes_status,
            "configSchemaVersion": self.config_schema_version,
        }


@dataclass(frozen=True)
class ConfigPreview:
    path: Path
    status: str
    source_version: int | None
    target_version: int
    target_declared_by_release: bool
    migration_required: bool
    migration_steps: tuple[str, ...]
    backup_required: bool
    backup_path_pattern: str | None
    recovery_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "status": self.status,
            "sourceVersion": self.source_version,
            "targetVersion": self.target_version,
            "targetDeclaredByRelease": self.target_declared_by_release,
            "migrationRequired": self.migration_required,
            "migrationSteps": list(self.migration_steps),
            "backupRequired": self.backup_required,
            "backupPathPattern": self.backup_path_pattern,
            "backupPolicy": (
                "timestamped byte-for-byte sibling backup before migrated config activation"
                if self.backup_required
                else None
            ),
            "recoveryRequired": self.recovery_required,
        }


@dataclass(frozen=True)
class UpdatePreview:
    current_version: str
    target_version: str
    channel: str
    ownership: InstallOwnership
    release: ReleasePreview
    config: ConfigPreview
    rollback_available: bool
    allowed: bool
    action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "currentVersion": self.current_version,
            "targetVersion": self.target_version,
            "channel": self.channel,
            "ownership": self.ownership.to_dict(),
            "release": self.release.to_dict(),
            "config": self.config.to_dict(),
            "rollback": {
                "availableNow": self.rollback_available,
                "policy": "retain the prior verified executable after activation",
            },
            "allowed": self.allowed,
            "action": self.action,
        }


@dataclass(frozen=True)
class _ReleaseSources:
    releases_api: str
    raw_base: str
    download_base: str


def _release_sources() -> _ReleaseSources:
    """Return production sources or an explicitly gated CI canary fixture.

    The override is deliberately unsuitable as a general mirror feature: all
    values must be provided together, CI and canary mode must both be explicit,
    and URLs cannot carry credentials, query secrets, or fragments. Plain HTTP
    is accepted only for a loopback fixture populated from authenticated draft
    release downloads.
    """
    values = {name: os.environ.get(env_name) for name, env_name in _CANARY_SOURCE_ENVS.items()}
    configured = {name for name, value in values.items() if value}
    if not configured:
        return _ReleaseSources(_RELEASES_API, _RAW_BASE, _RELEASE_DOWNLOAD_BASE)

    if configured != set(_CANARY_SOURCE_ENVS):
        raise UpdateError(
            "JARN-UPDATE-016: release-canary source overrides must be supplied together"
        )
    if os.environ.get(_CANARY_MODE_ENV) != "1" or os.environ.get("CI", "").lower() not in {
        "1",
        "true",
    }:
        raise UpdateError(
            "JARN-UPDATE-016: release-canary source overrides require explicit CI canary mode"
        )

    validated: dict[str, str] = {}
    for name, env_name in _CANARY_SOURCE_ENVS.items():
        value = values[name]
        assert value is not None
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        secure_transport = parsed.scheme == "https"
        loopback_fixture = parsed.scheme in {"http", "https"} and is_loopback
        if (
            not parsed.netloc
            or not (secure_transport or loopback_fixture)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise UpdateError(
                f"JARN-UPDATE-016: {env_name} must be credential-free HTTPS "
                "or a loopback HTTP(S) fixture"
            )
        validated[name] = value.rstrip("/")
    return _ReleaseSources(**validated)


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    channel: str
    update_available: bool
    source: str = _RELEASES_API

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "currentVersion": self.current_version,
            "latestVersion": self.latest_version,
            "channel": self.channel,
            "updateAvailable": self.update_available,
            "source": self.source,
        }


def _default_fetch_releases() -> list[dict[str, Any]]:
    sources = _release_sources()
    request = urllib.request.Request(
        sources.releases_api,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"jarn/{__version__}"},
    )
    with urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT) as response:
        payload = response.read(_MAX_RELEASE_CATALOG_BYTES + 1)
    if len(payload) > _MAX_RELEASE_CATALOG_BYTES:
        raise ValueError(f"release response exceeded {_MAX_RELEASE_CATALOG_BYTES} bytes")
    value = json.loads(payload)
    if not isinstance(value, list):
        raise ValueError("release response is not a list")
    return [item for item in value if isinstance(item, dict)]


def _fetch_release_catalog(
    *,
    channel: str,
    _fetch: Callable[[], list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    if channel not in {"stable", "beta"}:
        raise UpdateError("JARN-UPDATE-006: channel must be `stable` or `beta`")
    try:
        releases = (_fetch or _default_fetch_releases)()
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpdateError(
            f"JARN-UPDATE-007: could not check {channel} releases within "
            f"{_NETWORK_TIMEOUT:.0f}s: {exc}; check the network/proxy and retry"
        ) from exc
    return [release for release in releases if isinstance(release, dict)]


def _eligible_releases(
    releases: list[dict[str, Any]],
    *,
    channel: str,
) -> list[tuple[Version, dict[str, Any]]]:
    eligible: list[tuple[Version, dict[str, Any]]] = []
    for release in releases:
        if release.get("draft") is True:
            continue
        if channel == "stable" and release.get("prerelease") is True:
            continue
        tag = str(release.get("tag_name") or "").removeprefix("v")
        try:
            parsed = Version(tag)
        except InvalidVersion:
            continue
        eligible.append((parsed, release))
    return eligible


def _select_release(
    releases: list[dict[str, Any]],
    *,
    channel: str,
    requested_version: str | None = None,
) -> tuple[Version, dict[str, Any]]:
    eligible = _eligible_releases(releases, channel=channel)
    if not eligible:
        raise UpdateError(
            f"JARN-UPDATE-008: no valid {channel} release was returned; retry later or "
            "inspect https://github.com/chayapats/jarn/releases"
        )
    if requested_version is None:
        return max(eligible, key=lambda item: item[0])
    try:
        requested = Version(requested_version.removeprefix("v"))
    except InvalidVersion as exc:
        raise UpdateError(
            f"JARN-UPDATE-024: requested version is invalid: {requested_version}"
        ) from exc
    matches = [item for item in eligible if item[0] == requested]
    if len(matches) != 1:
        raise UpdateError(
            f"JARN-UPDATE-024: release {requested} was not found exactly once in the "
            f"primary {channel} release catalog; nothing was changed"
        )
    return matches[0]


def check_for_update(
    *,
    channel: str = "stable",
    current_version: str = __version__,
    _fetch: Callable[[], list[dict[str, Any]]] | None = None,
) -> UpdateInfo:
    """Return the newest eligible GitHub release for stable or beta."""
    releases = _fetch_release_catalog(channel=channel, _fetch=_fetch)
    latest, _release = _select_release(releases, channel=channel)
    try:
        current = Version(current_version)
    except InvalidVersion as exc:
        raise UpdateError(
            f"JARN-UPDATE-009: running version is invalid: {current_version}"
        ) from exc
    return UpdateInfo(
        current_version=str(current),
        latest_version=str(latest),
        channel=channel,
        update_available=latest > current,
    )


def _fetch_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"jarn/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT) as response:
            return response.read()
    except Exception as exc:  # noqa: BLE001
        raise UpdateError(
            f"JARN-UPDATE-010: could not download {url} within "
            f"{_NETWORK_TIMEOUT:.0f}s: {exc}; the current version was not changed"
        ) from exc


def _installer_checksum(manifest: bytes) -> str:
    try:
        text = manifest.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError(
            "JARN-UPDATE-011: release checksum manifest is not UTF-8; "
            "the current version was not changed"
        ) from exc
    matches: list[str] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == "install.sh":
            digest = fields[0].lower()
            if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
                matches.append(digest)
    if len(matches) != 1:
        raise UpdateError(
            "JARN-UPDATE-011: release checksums did not contain exactly one "
            "install.sh digest; the current version was not changed"
        )
    return matches[0]


def _download_installer(
    version: str,
    destination: Path,
    *,
    _fetch: Callable[[str], bytes] = _fetch_url,
) -> str:
    sources = _release_sources()
    url = f"{sources.raw_base}/v{version}/install.sh"
    checksums_url = f"{sources.download_base}/v{version}/checksums.txt"
    payload = _fetch(url)
    expected = _installer_checksum(_fetch(checksums_url))
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise UpdateError(
            "JARN-UPDATE-011: install.sh SHA-256 mismatch; refusing to execute it and "
            "leaving the current version unchanged"
        )
    if not payload.startswith(b"#!/bin/sh"):
        raise UpdateError(
            "JARN-UPDATE-011: downloaded installer had an unexpected format; "
            "the current version was not changed"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError(
            "JARN-UPDATE-011: downloaded installer is not UTF-8; "
            "the current version was not changed"
        ) from exc
    atomic_write_text(destination, text, mode=0o700)
    return url


def _manager_command(kind: str, target: str) -> tuple[str, ...] | None:
    commands = {
        "uv": ("uv", "tool", "install", "--force", f"jarn=={target}"),
        "pipx": ("pipx", "install", "--force", f"jarn=={target}"),
        "pip": ("python3", "-m", "pip", "install", "--upgrade", f"jarn=={target}"),
        "npm": ("npm", "install", "--global", f"jarn-cli@{target}"),
        "homebrew": ("brew", "upgrade", "jarn"),
    }
    return commands.get(kind)


def _ownership_from_record(record: InstallRecord, target: str) -> InstallOwnership:
    method = record.method.strip().lower().replace("_", "-")
    if method in {"binary", "python"}:
        return InstallOwnership(
            kind="curl-managed",
            method=method,
            source="validated install record",
            active_path=record.active_path,
            updater_managed=True,
            installer_method=method,
        )
    aliases = {
        "uv": "uv",
        "uv-tool": "uv",
        "uvtool": "uv",
        "pipx": "pipx",
        "pip": "pip",
        "pip-user": "pip",
        "pipuser": "pip",
        "npm": "npm",
        "node": "npm",
        "jarn-cli": "npm",
        "brew": "homebrew",
        "homebrew": "homebrew",
    }
    kind = aliases.get(method, "unmanaged")
    return InstallOwnership(
        kind=kind,
        method=record.method,
        source="validated install record",
        active_path=record.active_path,
        updater_managed=False,
        manager_command=_manager_command(kind, target),
    )


def _ownership_from_path(active_path: Path | None, target: str) -> InstallOwnership:
    if active_path is None:
        return InstallOwnership(
            kind="unmanaged",
            method="unknown",
            source="no install record or resolvable jarn command",
            active_path=None,
            updater_managed=False,
        )
    candidates = [str(active_path).lower()]
    with contextlib.suppress(OSError):
        candidates.append(str(active_path.resolve()).lower())
    joined = "\n".join(candidates).replace("\\", "/")
    script_prefix = ""
    with contextlib.suppress(OSError, UnicodeDecodeError):
        if active_path.is_file() and active_path.stat().st_size <= 1024 * 1024:
            with active_path.open("rb") as stream:
                script_prefix = stream.read(4096).decode("utf-8")
    if "/uv/tools/" in joined or "/.local/share/uv/tools/" in joined:
        kind = "uv"
    elif "/pipx/venvs/" in joined or "/.local/share/pipx/" in joined:
        kind = "pipx"
    elif "/node_modules/" in joined:
        kind = "npm"
    elif "/cellar/" in joined or "/homebrew/" in joined or "/linuxbrew/" in joined:
        kind = "homebrew"
    elif "/site-packages/" in joined or (
        script_prefix.startswith("#!")
        and "python" in script_prefix.splitlines()[0].lower()
        and ("from jarn." in script_prefix or "import jarn" in script_prefix)
    ):
        kind = "pip"
    elif (
        script_prefix.startswith("#!")
        and "node" in script_prefix.splitlines()[0].lower()
        and ("jarn-cli" in script_prefix or "node_modules" in script_prefix)
    ):
        kind = "npm"
    else:
        kind = "unmanaged"
    return InstallOwnership(
        kind=kind,
        method=kind if kind != "unmanaged" else "unknown",
        source="executable-path inference; install record absent",
        active_path=active_path,
        updater_managed=False,
        manager_command=_manager_command(kind, target),
    )


def _install_context(
    *,
    manifest_path: Path,
    target: str,
    active_path: Path | None,
) -> tuple[InstallRecord | None, InstallOwnership, str | None]:
    if manifest_path.exists() or manifest_path.is_symlink():
        try:
            record = load_actionable_install_record(manifest_path)
        except InstallStateError as exc:
            ownership = InstallOwnership(
                kind="invalid-record",
                method="unknown",
                source="install record failed actionable validation",
                active_path=active_path,
                updater_managed=False,
            )
            return None, ownership, str(exc)
        ownership = _ownership_from_record(record, target)
        actual = _version_from_command(record.active_path)
        if actual is None:
            return (
                record,
                ownership,
                "JARN-UPDATE-025: the executable named by the validated install record "
                "did not pass `--version`; nothing was changed",
            )
        try:
            version_matches = Version(actual) == Version(record.version)
        except InvalidVersion:
            version_matches = False
        if not version_matches:
            return (
                record,
                ownership,
                "JARN-UPDATE-025: the validated install record version does not match "
                "its active executable; run `jarn doctor --report` before updating",
            )
        return record, ownership, None

    resolved = active_path
    if resolved is None:
        command = shutil.which("jarn")
        resolved = Path(command) if command else None
    return None, _ownership_from_path(resolved, target), None


def _bounded_release_text(value: Any, *, limit: int, single_line: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    text = redact_secrets(value.replace("\r\n", "\n").replace("\r", "\n"))
    # Release metadata is untrusted presentation data. Keep newlines/tabs in
    # notes but remove terminal escapes and the rest of the control range.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    if single_line:
        text = " ".join(text.split())
    return text[:limit]


def _breaking_changes(notes: str) -> tuple[str, ...]:
    items: list[str] = []
    in_breaking_section = False
    for raw_line in notes.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            heading_text = heading.group(1).strip().lower()
            in_breaking_section = "breaking" in heading_text
            continue
        explicit = re.match(r"^(?:[-*]\s*)?breaking(?:\s+change)?\s*:\s*(.+)$", line, re.I)
        candidate = explicit.group(1) if explicit else line if in_breaking_section else ""
        candidate = re.sub(r"^[-*+]\s+", "", candidate).strip()
        if not candidate:
            continue
        candidate = candidate[:_MAX_BREAKING_ITEM_CHARS]
        if candidate not in items:
            items.append(candidate)
        if len(items) >= _MAX_BREAKING_ITEMS:
            break
    return tuple(items)


def _release_preview(version: str, release: dict[str, Any]) -> ReleasePreview:
    raw_body = release.get("body")
    raw_notes = raw_body if isinstance(raw_body, str) else ""
    redacted_notes = _bounded_release_text(raw_notes, limit=_MAX_RELEASE_NOTES_CHARS + 1)
    notes_truncated = len(redacted_notes) > _MAX_RELEASE_NOTES_CHARS
    notes = redacted_notes[:_MAX_RELEASE_NOTES_CHARS]
    breaking = _breaking_changes(notes)
    schema_match = re.search(
        r"(?:JARN-CONFIG-SCHEMA|config\s+schema(?:\s+version)?)\s*:\s*(\d{1,4})",
        raw_notes,
        re.I,
    )
    declared_schema = int(schema_match.group(1)) if schema_match else None
    title = (
        _bounded_release_text(release.get("name"), limit=_MAX_RELEASE_TITLE_CHARS, single_line=True)
        or f"J.A.R.N. {version}"
    )
    published_at = release.get("published_at")
    if not isinstance(published_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T[0-9:.+-]+Z?", published_at
    ):
        published_at = None
    return ReleasePreview(
        version=version,
        title=title,
        url=f"https://github.com/chayapats/jarn/releases/tag/v{version}",
        published_at=published_at,
        notes=notes,
        notes_truncated=notes_truncated,
        breaking_changes=breaking,
        breaking_changes_status="declared" if breaking else "none-declared",
        config_schema_version=declared_schema,
    )


def _config_preview(release: ReleasePreview, *, path: Path) -> ConfigPreview:
    diagnostic = diagnose_config_file(path)
    declared = release.config_schema_version is not None
    target = release.config_schema_version if declared else CURRENT_CONFIG_VERSION
    assert target is not None
    source = diagnostic.source_version
    steps: tuple[str, ...] = ()
    if diagnostic.status == "migration-required":
        with contextlib.suppress(Exception):
            steps = plan_config_migration(path).summary
    if source is not None and target > CURRENT_CONFIG_VERSION:
        start = max(source, CURRENT_CONFIG_VERSION)
        steps = (*steps, *(f"schema {value} -> {value + 1}" for value in range(start, target)))
    elif source is not None and target < source:
        steps = (f"schema {source} -> {target} requires release-specific downgrade guidance",)
    migration_required = bool(steps) and steps != ("schema already current",)
    recovery_required = diagnostic.status in {"corrupt", "invalid", "unsupported"} or (
        source is not None and target < source
    )
    backup_required = path.exists() and migration_required
    return ConfigPreview(
        path=path,
        status=diagnostic.status,
        source_version=source,
        target_version=target,
        target_declared_by_release=declared,
        migration_required=migration_required,
        migration_steps=steps,
        backup_required=backup_required,
        backup_path_pattern=f"{path}.bak.<UTC timestamp>" if backup_required else None,
        recovery_required=recovery_required,
    )


def _ownership_action(ownership: InstallOwnership, target: str) -> tuple[bool, str]:
    if ownership.updater_managed and ownership.installer_method:
        return True, f"run the verified installer with --method {ownership.installer_method}"
    if ownership.manager_command:
        command = " ".join(ownership.manager_command)
        return (
            False,
            f"refused ownership change; update with `{command}`. To migrate ownership, "
            "review this dry-run and then invoke the official curl installer separately",
        )
    if ownership.kind == "invalid-record":
        return False, "repair the invalid install record before any update"
    return (
        False,
        "installation ownership is unproven; use the owning package manager or explicitly "
        "migrate with the official curl installer after reviewing its dry-run",
    )


def _emit_preview(preview: UpdatePreview) -> None:
    print("Update preview (no changes yet)")
    print(f"  Version: {preview.current_version} -> {preview.target_version}")
    print(
        f"  Installation owner: {preview.ownership.kind} "
        f"({preview.ownership.method}; {preview.ownership.source})"
    )
    print(f"  Release: {preview.release.title} ({preview.release.url})")
    if preview.release.notes:
        print("  Release notes:")
        for line in preview.release.notes.splitlines():
            print(f"    {line}")
        if preview.release.notes_truncated:
            print("    [notes truncated]")
    if preview.release.breaking_changes:
        print("  Breaking changes:")
        for item in preview.release.breaking_changes:
            print(f"    - {item}")
    else:
        print("  Breaking changes: none declared in the release metadata")
    config = preview.config
    print(
        f"  Config: schema {config.source_version if config.source_version is not None else 'n/a'} "
        f"-> {config.target_version} ({config.status})"
    )
    for step in config.migration_steps:
        print(f"    - {step}")
    if config.backup_required:
        print(f"  Config backup before migration: {config.backup_path_pattern}")
    if config.recovery_required:
        print("  Config recovery review is required before the new version can use this file")
    print(
        "  Rollback: prior verified executable is already retained"
        if preview.rollback_available
        else "  Rollback: updater will retain the current verified executable before activation"
    )
    print(f"  Action: {preview.action}")


def run_update(
    *,
    channel: str = "stable",
    check_only: bool = False,
    as_json: bool = False,
    dry_run: bool = False,
    version: str | None = None,
    _fetch: Callable[[], list[dict[str, Any]]] | None = None,
    _runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    _download: Callable[[str, Path], str] = _download_installer,
    _manifest_path: Path | None = None,
    _active_path: Path | None = None,
    _config_path: Path | None = None,
) -> int:
    """Check or install an update while preserving installation ownership."""
    try:
        releases = _fetch_release_catalog(channel=channel, _fetch=_fetch)
        latest, latest_release = _select_release(releases, channel=channel)
        info = UpdateInfo(
            current_version=str(Version(__version__)),
            latest_version=str(latest),
            channel=channel,
            update_available=latest > Version(__version__),
            source=_release_sources().releases_api,
        )
    except UpdateError as exc:
        _emit_update_error(exc, as_json=as_json)
        return 1
    except InvalidVersion:
        _emit_update_error(
            UpdateError(f"JARN-UPDATE-009: running version is invalid: {__version__}"),
            as_json=as_json,
        )
        return 1

    if check_only:
        if as_json:
            print(json.dumps(info.to_dict(), ensure_ascii=False, sort_keys=True))
        elif info.update_available:
            print(
                f"J.A.R.N. {info.latest_version} is available ({channel}); current {info.current_version}."
            )
        else:
            print(f"J.A.R.N. {info.current_version} is current on the {channel} channel.")
        return 0

    try:
        selected, selected_release = (
            (latest, latest_release)
            if version is None
            else _select_release(releases, channel=channel, requested_version=version)
        )
    except UpdateError as exc:
        _emit_update_error(exc, as_json=as_json)
        return 1
    target = str(selected)
    manifest = _manifest_path or default_manifest_path()
    record, ownership, context_error = _install_context(
        manifest_path=manifest,
        target=target,
        active_path=_active_path,
    )
    inferred_version = (
        _version_from_command(ownership.active_path) if ownership.active_path is not None else None
    )
    current = record.version if record is not None else inferred_version or info.current_version

    if context_error is None:
        try:
            selected_is_current = selected == Version(current)
            selected_is_older = selected < Version(current)
        except InvalidVersion:
            context_error = (
                f"JARN-UPDATE-025: installed version is invalid: {current}; "
                "run `jarn doctor --report` before updating"
            )
            selected_is_current = False
            selected_is_older = False
    else:
        selected_is_current = False
        selected_is_older = False

    if context_error is None and (selected_is_current or (version is None and selected_is_older)):
        current_info = UpdateInfo(
            current_version=str(Version(current)),
            latest_version=target,
            channel=channel,
            update_available=False,
            source=info.source,
        )
        if as_json:
            print(json.dumps({**current_info.to_dict(), "changed": False}, sort_keys=True))
        else:
            print(f"J.A.R.N. {current_info.current_version} is already current ({channel}).")
        return 0

    release = _release_preview(target, selected_release)
    try:
        config = _config_preview(release, path=_config_path or global_config_path())
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary must fail closed
        _emit_update_error(
            UpdateError(
                "JARN-UPDATE-026: configuration migration preview could not be created; "
                f"nothing was changed ({redact_secrets(str(exc))})"
            ),
            as_json=as_json,
        )
        return 1
    allowed, action = _ownership_action(ownership, target)
    preview = UpdatePreview(
        current_version=current,
        target_version=target,
        channel=channel,
        ownership=ownership,
        release=release,
        config=config,
        rollback_available=bool(record and record.previous_path),
        allowed=allowed and context_error is None,
        action=action,
    )

    if not as_json:
        _emit_preview(preview)
    if context_error is not None:
        _emit_update_error(UpdateError(context_error), as_json=as_json, preview=preview)
        return 1
    if not allowed:
        _emit_update_error(
            UpdateError(f"JARN-UPDATE-025: {action}; nothing was changed"),
            as_json=as_json,
            preview=preview,
        )
        return 1

    sh = shutil.which("sh")
    if sh is None:
        _emit_update_error(
            UpdateError("JARN-UPDATE-012: POSIX sh is required for the supported updater"),
            as_json=as_json,
            preview=preview,
        )
        return 1
    assert ownership.installer_method is not None

    with tempfile.TemporaryDirectory(prefix="jarn-update-") as temp_dir:
        installer = Path(temp_dir) / "install.sh"
        try:
            source = _download(target, installer)
        except UpdateError as exc:
            _emit_update_error(exc, as_json=as_json, preview=preview)
            return 1
        argv = [
            sh,
            str(installer),
            "--version",
            target,
            "--channel",
            channel,
            "--no-setup",
            "--yes",
            "--method",
            ownership.installer_method,
        ]
        if dry_run:
            argv.append("--dry-run")
        if not as_json:
            print(f"Updating from verified source: {source}")
            print(
                "The current executable remains available until the candidate passes verification."
            )
        try:
            runner_kwargs: dict[str, Any] = {
                "check": False,
                "env": _independent_executable_env(),
            }
            # A machine-readable command must emit exactly one JSON document;
            # keep the installer's human stage log out of stdout in this mode.
            if as_json:
                runner_kwargs.update(capture_output=True, text=True)
            completed = _runner(argv, **runner_kwargs)
        except OSError as exc:
            _emit_update_error(
                UpdateError(
                    f"JARN-UPDATE-013: updater could not start: {exc}; nothing was changed"
                ),
                as_json=as_json,
                preview=preview,
            )
            return 1

    rc = int(completed.returncode)
    success = rc in {0, 10}
    if as_json:
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "ok": success,
                    "changed": success and not dry_run,
                    "version": target,
                    "channel": channel,
                    "activationRequired": rc == 10,
                    "installerExitCode": rc,
                    "preview": preview.to_dict(),
                },
                sort_keys=True,
            )
        )
    if not success and not as_json:
        print(
            "JARN-UPDATE-014: update did not complete; the installer preserved or restored "
            "the previous executable. Run `jarn doctor --report`.",
            file=sys.stderr,
        )
    return rc


def _emit_update_error(
    exc: Exception,
    *,
    as_json: bool,
    preview: UpdatePreview | None = None,
) -> None:
    message = str(exc)
    if as_json:
        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "ok": False,
            "changed": False,
            "error": {"message": message},
        }
        if preview is not None:
            payload["preview"] = preview.to_dict()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(message, file=sys.stderr)


def _smoke(path: Path) -> tuple[bool, str]:
    if not (path.exists() or path.is_symlink()):
        return False, "path does not exist"
    for args in (["--version"], ["--help"]):
        try:
            result = subprocess.run(
                [str(path), *args],
                capture_output=True,
                env=_independent_executable_env(),
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
    return True, ""


def run_rollback(
    *,
    manifest_path: Path | None = None,
    as_json: bool = False,
    _smoke_check: Callable[[Path], tuple[bool, str]] = _smoke,
) -> int:
    """Atomically exchange the active executable with the retained prior one."""
    manifest = manifest_path or default_manifest_path()
    try:
        record = load_actionable_install_record(manifest)
    except InstallStateError as exc:
        _emit_update_error(exc, as_json=as_json)
        return 1
    previous = record.previous_path
    active = record.active_path
    if previous is None:
        _emit_update_error(
            UpdateError(
                "JARN-UPDATE-015: no retained previous version is available to roll back to"
            ),
            as_json=as_json,
        )
        return 1
    if active.parent.resolve() != previous.parent.resolve():
        _emit_update_error(
            UpdateError("JARN-UPDATE-016: rollback paths are not on the same managed filesystem"),
            as_json=as_json,
        )
        return 1
    ok, reason = _smoke_check(previous)
    if not ok:
        _emit_update_error(
            UpdateError(
                f"JARN-UPDATE-017: retained rollback candidate failed verification: {reason}; "
                "the active version was not changed"
            ),
            as_json=as_json,
        )
        return 1

    exchange = active.with_name(f".jarn.rollback-exchange.{os.getpid()}")
    if exchange.exists() or exchange.is_symlink():
        _emit_update_error(
            UpdateError(f"JARN-UPDATE-018: rollback staging path already exists: {exchange}"),
            as_json=as_json,
        )
        return 1

    with file_lock(manifest):
        try:
            os.replace(active, exchange)
            os.replace(previous, active)
        except OSError as exc:
            if exchange.exists() or exchange.is_symlink():
                with contextlib.suppress(OSError):
                    os.replace(exchange, active)
            _emit_update_error(
                UpdateError(f"JARN-UPDATE-019: rollback activation failed: {exc}"),
                as_json=as_json,
            )
            return 1

        ok, reason = _smoke_check(active)
        if not ok:
            try:
                os.replace(active, previous)
                os.replace(exchange, active)
            except OSError as restore_exc:
                _emit_update_error(
                    UpdateError(
                        f"JARN-UPDATE-020: rollback verification failed ({reason}) and automatic "
                        f"restore failed ({restore_exc}); inspect {active.parent}"
                    ),
                    as_json=as_json,
                )
                return 1
            _emit_update_error(
                UpdateError(
                    f"JARN-UPDATE-021: rollback candidate failed after activation ({reason}); "
                    "the prior active executable was restored"
                ),
                as_json=as_json,
            )
            return 1

        old_version = record.version
        rolled_version = _version_from_command(active) or "unknown"
        updated = InstallRecord(
            schema_version=record.schema_version,
            version=rolled_version,
            method=record.method,
            channel=record.channel,
            active_path=active,
            candidate_path=active,
            previous_path=previous,
            state_dir=record.state_dir,
            platform=record.platform,
            dependency=record.dependency,
            activation=record.activation,
            setup_status=record.setup_status,
            installed_at=record.installed_at,
        )
        retained = False
        try:
            os.replace(exchange, previous)
            retained = True
            atomic_write_text(
                manifest,
                json.dumps(updated.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                mode=0o600,
            )
        except OSError as exc:
            try:
                if retained:
                    # active=candidate, previous=old-active: exchange them back.
                    os.replace(active, exchange)
                    os.replace(previous, active)
                    os.replace(exchange, previous)
                else:
                    # active=candidate, exchange=old-active, previous is vacant.
                    os.replace(active, previous)
                    os.replace(exchange, active)
            except OSError as restore_exc:
                _emit_update_error(
                    UpdateError(
                        f"JARN-UPDATE-022: rollback metadata failed ({exc}) and automatic "
                        f"restore failed ({restore_exc}); inspect {active.parent}"
                    ),
                    as_json=as_json,
                )
                return 1
            _emit_update_error(
                UpdateError(
                    f"JARN-UPDATE-023: rollback metadata could not be committed ({exc}); "
                    "the original active and retained versions were restored"
                ),
                as_json=as_json,
            )
            return 1

    payload = {
        "schemaVersion": 1,
        "ok": True,
        "activeVersion": rolled_version,
        "previousVersion": old_version,
        "activePath": str(active),
    }
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Rolled back to J.A.R.N. {rolled_version} at {active}.")
        print(f"J.A.R.N. {old_version} is retained for a forward rollback at {previous}.")
    return 0


def _version_from_command(path: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_independent_executable_env(),
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return first.removeprefix("jarn ").strip() or None
