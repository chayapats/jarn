"""Safe user-space acquisition of the official standalone Codex CLI.

The installer deliberately uses the same release package published by OpenAI's
standalone installer, without requiring Node/npm.  Release metadata and the
checksum manifest are both authenticated by their SHA-256 digests, extraction
is confined to a staging directory, and activation happens only after a version
smoke test succeeds.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jarn.config.secrets import redact_secrets
from jarn.util.atomic import file_lock
from jarn.version import __version__

CODEX_MINIMUM_VERSION = "0.100.0"
CODEX_RELEASE_CHANNEL = "latest"
CODEX_RELEASE_METADATA_URL = "https://releases.openai.com/codex/channels/latest"
CODEX_GITHUB_METADATA_URL = "https://api.github.com/repos/openai/codex/releases/latest"
CODEX_OFFICIAL_INSTALL_COMMAND = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"

_MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 768 * 1024 * 1024
_NETWORK_TIMEOUT_SECONDS = 30.0
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_VERSION_OUTPUT_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "releases.openai.com",
        "github.com",
        "objects.githubusercontent.com",
    }
)


class CodexDependencyInstallError(RuntimeError):
    """The standalone dependency could not be safely installed."""

    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        self.detail = redact_secrets(message)
        super().__init__(f"Codex install failed during {stage}: {self.detail}")


@dataclass(frozen=True, slots=True)
class CodexInstallPlan:
    """Resolved, user-displayable description of an external install."""

    version: str
    target: str
    asset_name: str
    asset_url: str
    asset_sha256: str
    checksum_url: str
    checksum_sha256: str
    source: str
    metadata_url: str
    destination: str
    release_directory: str
    channel: str = CODEX_RELEASE_CHANNEL
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": "OpenAI Codex CLI",
            "purpose": "ChatGPT subscription authentication and model access",
            "version": self.version,
            "channel": self.channel,
            "target": self.target,
            "source": self.source,
            "metadata_url": self.metadata_url,
            "asset": self.asset_name,
            "asset_url": self.asset_url,
            "destination": self.destination,
            "release_directory": self.release_directory,
            "verification": {
                "algorithm": "sha256",
                "release_digest": self.asset_sha256,
                "checksum_manifest_url": self.checksum_url,
                "checksum_manifest_digest": self.checksum_sha256,
                "signature": "not_published_for_standalone_package",
            },
        }


@dataclass(frozen=True, slots=True)
class CodexInstallResult:
    plan: CodexInstallPlan
    executable: str
    smoke_version: str
    changed: bool
    previous_version: str | None = None
    backup_path: str | None = None
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": "installed" if self.changed else "already_current",
            "changed": self.changed,
            "executable": self.executable,
            "version": self.smoke_version,
            "previous_version": self.previous_version,
            "backup_path": self.backup_path,
            "plan": self.plan.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class _PathBackup:
    kind: str
    symlink_target: str | None = None
    file_copy: Path | None = None


def _codex_home(home: Path | None = None) -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return (home or Path.home()) / ".codex"


def managed_codex_executable(*, home: Path | None = None) -> Path | None:
    """Return an active standalone executable even when ``~/.local/bin`` is not on PATH."""

    candidate = _codex_home(home) / "packages" / "standalone" / "current" / "bin" / "codex"
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    except OSError:
        return None
    return None


def _platform_target(
    system: str | None = None,
    machine: str | None = None,
) -> str:
    system_value = (system or platform.system()).lower()
    machine_value = (machine or platform.machine()).lower()
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine_value)
    if arch is None:
        raise CodexDependencyInstallError(
            "platform", f"unsupported CPU architecture: {machine_value or 'unknown'}"
        )
    if system_value == "linux":
        # The official standalone package is musl-linked so it also works on
        # older glibc hosts (the exact failure that motivated this install path).
        return f"{arch}-unknown-linux-musl"
    if system_value == "darwin":
        return f"{arch}-apple-darwin"
    alternative = (
        " Run J.A.R.N. inside WSL for the supported Linux install path."
        if system_value == "windows"
        else ""
    )
    raise CodexDependencyInstallError(
        "platform",
        f"standalone Codex installation is supported on macOS and Linux, not {system_value}."
        f"{alternative}",
    )


def _sha256_digest(value: object, *, label: str) -> str:
    text = str(value or "")
    if text.startswith("sha256:"):
        text = text.removeprefix("sha256:")
    text = text.lower()
    if not _SHA256_RE.fullmatch(text):
        raise CodexDependencyInstallError("metadata", f"{label} has no valid SHA-256 digest")
    return text


def _safe_download_url(value: object, *, label: str) -> str:
    url = str(value or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
        raise CodexDependencyInstallError(
            "metadata", f"{label} URL is not an allowed official HTTPS source"
        )
    if parsed.username or parsed.password:
        raise CodexDependencyInstallError("metadata", f"{label} URL contains credentials")
    return url


def _release_version(metadata: dict[str, Any]) -> str:
    tag = str(metadata.get("tag_name") or "")
    version = tag.removeprefix("rust-v").removeprefix("v")
    if not _VERSION_RE.fullmatch(version):
        raise CodexDependencyInstallError("metadata", "release tag has an invalid version")
    return version


def _asset(metadata: dict[str, Any], name: str) -> dict[str, Any]:
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise CodexDependencyInstallError("metadata", "release assets are missing")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise CodexDependencyInstallError(
            "metadata", f"release must contain exactly one {name!r} asset"
        )
    return matches[0]


class CodexDependencyInstaller:
    """Resolve and transactionally activate the official standalone package."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        codex_home: Path | None = None,
        destination: Path | None = None,
        system: str | None = None,
        machine: str | None = None,
        timeout_seconds: float = _NETWORK_TIMEOUT_SECONDS,
        fetch_bytes: Callable[[str], bytes] | None = None,
        download_file: Callable[[str, Path], str] | None = None,
        smoke: Callable[[Path], str] | None = None,
    ) -> None:
        self.home = home or Path.home()
        self.codex_home = codex_home or _codex_home(self.home)
        self.destination = destination or self.home / ".local" / "bin" / "codex"
        self.target = _platform_target(system, machine)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._fetch_bytes_override = fetch_bytes
        self._download_file_override = download_file
        self._smoke_override = smoke
        self.standalone_root = self.codex_home / "packages" / "standalone"

    def _fetch_bytes(self, url: str, *, maximum: int = _MAX_METADATA_BYTES) -> bytes:
        if self._fetch_bytes_override is not None:
            try:
                payload = self._fetch_bytes_override(url)
            except CodexDependencyInstallError:
                raise
            except Exception as exc:  # noqa: BLE001 - injectable network boundary
                raise CodexDependencyInstallError(
                    "download", f"could not fetch {url}: {exc}"
                ) from exc
            if len(payload) > maximum:
                raise CodexDependencyInstallError("download", "response exceeded safety limit")
            return payload
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json, application/json, text/plain",
                "User-Agent": f"jarn/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(maximum + 1)
        except Exception as exc:  # noqa: BLE001 - normalized at the network boundary
            raise CodexDependencyInstallError("download", f"could not fetch {url}: {exc}") from exc
        if len(payload) > maximum:
            raise CodexDependencyInstallError("download", "response exceeded safety limit")
        return payload

    def _metadata(self, url: str) -> dict[str, Any]:
        try:
            value = json.loads(self._fetch_bytes(url))
        except CodexDependencyInstallError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexDependencyInstallError(
                "metadata", f"invalid release metadata: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise CodexDependencyInstallError("metadata", "release metadata is not an object")
        return value

    def resolve_plan(self) -> CodexInstallPlan:
        """Resolve latest metadata, preferring OpenAI Releases then GitHub."""

        errors: list[str] = []
        for metadata_url in (CODEX_RELEASE_METADATA_URL, CODEX_GITHUB_METADATA_URL):
            try:
                metadata = self._metadata(metadata_url)
                version = _release_version(metadata)
                asset_name = f"codex-package-{self.target}.tar.gz"
                archive = _asset(metadata, asset_name)
                checksums = _asset(metadata, "codex-package_SHA256SUMS")
                asset_url = _safe_download_url(
                    archive.get("browser_download_url"), label=asset_name
                )
                checksum_url = _safe_download_url(
                    checksums.get("browser_download_url"), label="checksum manifest"
                )
                release_dir = self.standalone_root / "releases" / f"{version}-{self.target}"
                return CodexInstallPlan(
                    version=version,
                    target=self.target,
                    asset_name=asset_name,
                    asset_url=asset_url,
                    asset_sha256=_sha256_digest(archive.get("digest"), label=asset_name),
                    checksum_url=checksum_url,
                    checksum_sha256=_sha256_digest(
                        checksums.get("digest"), label="checksum manifest"
                    ),
                    source=(
                        "OpenAI Releases"
                        if urllib.parse.urlparse(metadata_url).hostname == "releases.openai.com"
                        else "openai/codex GitHub release"
                    ),
                    metadata_url=metadata_url,
                    destination=str(self.destination),
                    release_directory=str(release_dir),
                )
            except CodexDependencyInstallError as exc:
                errors.append(f"{metadata_url}: {exc.detail}")
        raise CodexDependencyInstallError(
            "metadata",
            "official release metadata was unavailable or incomplete; "
            + "; ".join(errors)
            + f". Manual fallback: {CODEX_OFFICIAL_INSTALL_COMMAND}",
        )

    def _download_file(self, url: str, destination: Path) -> str:
        if self._download_file_override is not None:
            try:
                return self._download_file_override(url, destination).lower()
            except CodexDependencyInstallError:
                raise
            except Exception as exc:  # noqa: BLE001 - injectable network boundary
                destination.unlink(missing_ok=True)
                raise CodexDependencyInstallError(
                    "download", f"could not download {url}: {exc}"
                ) from exc
        request = urllib.request.Request(url, headers={"User-Agent": f"jarn/{__version__}"})
        digest = hashlib.sha256()
        total = 0
        try:
            with (
                urllib.request.urlopen(request, timeout=self.timeout_seconds) as response,
                destination.open("xb") as handle,
            ):
                os.chmod(destination, 0o600)
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_ARCHIVE_BYTES:
                        raise CodexDependencyInstallError(
                            "download", "Codex archive exceeded safety limit"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except CodexDependencyInstallError:
            destination.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - normalized at the network/filesystem boundary
            destination.unlink(missing_ok=True)
            raise CodexDependencyInstallError(
                "download", f"could not download {url}: {exc}"
            ) from exc
        return digest.hexdigest()

    @staticmethod
    def _manifest_digest(payload: bytes, asset_name: str) -> str:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodexDependencyInstallError("checksum", "checksum manifest is not UTF-8") from exc
        matches: list[str] = []
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            filename = parts[-1].removeprefix("*")
            if filename == asset_name and _SHA256_RE.fullmatch(parts[0].lower()):
                matches.append(parts[0].lower())
        if len(matches) != 1:
            raise CodexDependencyInstallError(
                "checksum", f"checksum manifest has no unique entry for {asset_name}"
            )
        return matches[0]

    @staticmethod
    def _validate_archive_members(archive: tarfile.TarFile) -> None:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise CodexDependencyInstallError(
                    "extract", f"archive contains unsafe path: {member.name}"
                )
            if member.ischr() or member.isblk() or member.isfifo():
                raise CodexDependencyInstallError(
                    "extract", f"archive contains unsupported special file: {member.name}"
                )
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                joined = target if target.is_absolute() else name.parent / target
                if target.is_absolute() or ".." in joined.parts:
                    raise CodexDependencyInstallError(
                        "extract", f"archive link escapes staging: {member.name}"
                    )

    def _extract(self, archive_path: Path, staging: Path) -> Path:
        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                self._validate_archive_members(archive)
                archive.extractall(staging, filter="data")
        except CodexDependencyInstallError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise CodexDependencyInstallError("extract", f"invalid Codex archive: {exc}") from exc
        direct = staging / "bin" / "codex"
        if direct.is_file():
            return staging
        candidates = list(staging.glob("*/bin/codex"))
        if len(candidates) != 1:
            raise CodexDependencyInstallError(
                "extract", "archive does not contain exactly one bin/codex executable"
            )
        return candidates[0].parent.parent

    def _smoke(self, executable: Path, expected_version: str) -> str:
        if self._smoke_override is not None:
            output = self._smoke_override(executable)
        else:
            try:
                completed = subprocess.run(  # noqa: S603 - fixed executable/argument, no shell
                    [str(executable), "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise CodexDependencyInstallError("smoke_test", str(exc)) from exc
            output = "\n".join(
                part for part in (completed.stdout, completed.stderr) if part
            ).strip()
            if completed.returncode != 0:
                raise CodexDependencyInstallError(
                    "smoke_test", output or f"codex --version exited {completed.returncode}"
                )
        match = _VERSION_OUTPUT_RE.search(output)
        actual = match.group(1) if match else ""
        if actual != expected_version:
            raise CodexDependencyInstallError(
                "smoke_test",
                f"expected Codex {expected_version}, but candidate reported {actual or 'unknown'}",
            )
        return actual

    @staticmethod
    def _capture(path: Path, backup_dir: Path) -> _PathBackup:
        if path.is_symlink():
            return _PathBackup("symlink", symlink_target=os.readlink(path))
        if not path.exists():
            return _PathBackup("missing")
        if not path.is_file():
            raise CodexDependencyInstallError(
                "activate", f"refusing to replace non-file destination: {path}"
            )
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_dir / f"{path.name}.{uuid.uuid4().hex}.previous"
        shutil.copy2(path, backup)
        return _PathBackup("file", file_copy=backup)

    @staticmethod
    def _publish_symlink(path: Path, target: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.symlink_to(target)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _restore(cls, path: Path, backup: _PathBackup) -> None:
        if backup.kind == "missing":
            path.unlink(missing_ok=True)
        elif backup.kind == "symlink":
            assert backup.symlink_target is not None
            cls._publish_symlink(path, backup.symlink_target)
        else:
            assert backup.file_copy is not None
            os.replace(backup.file_copy, path)

    @staticmethod
    def _existing_version(path: Path) -> str | None:
        if not (path.exists() or path.is_symlink()):
            return None
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argument, no shell
                [str(path), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        match = _VERSION_OUTPUT_RE.search(output)
        return match.group(1) if match else None

    def _is_activated(self, release: Path) -> bool:
        current = self.standalone_root / "current"
        try:
            return (
                current.is_symlink()
                and current.resolve(strict=False) == release.resolve(strict=False)
                and self.destination.is_symlink()
                and self.destination.resolve(strict=False)
                == (release / "bin" / "codex").resolve(strict=False)
            )
        except OSError:
            return False

    @staticmethod
    def _receipt_matches(release: Path, plan: CodexInstallPlan) -> bool:
        try:
            value = json.loads((release / ".jarn-install.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(value, dict) and all(
            value.get(key) == expected
            for key, expected in {
                "schema_version": 1,
                "version": plan.version,
                "target": plan.target,
                "asset": plan.asset_name,
                "asset_sha256": plan.asset_sha256,
                "checksum_manifest_sha256": plan.checksum_sha256,
            }.items()
        )

    @staticmethod
    def _write_receipt(release: Path, plan: CodexInstallPlan) -> None:
        receipt = release / ".jarn-install.json"
        temporary = release / f".jarn-install.{uuid.uuid4().hex}.tmp"
        payload = {
            "schema_version": 1,
            "version": plan.version,
            "target": plan.target,
            "asset": plan.asset_name,
            "asset_sha256": plan.asset_sha256,
            "checksum_manifest_sha256": plan.checksum_sha256,
            "source": plan.source,
        }
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, receipt)
        except OSError as exc:
            raise CodexDependencyInstallError(
                "stage", f"could not record verified package receipt: {exc}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def install(
        self,
        plan: CodexInstallPlan | None = None,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> CodexInstallResult:
        """Install/update only after checksum, extraction, and smoke verification."""

        resolved = plan or self.resolve_plan()
        expected_release = (
            self.standalone_root / "releases" / (f"{resolved.version}-{resolved.target}")
        )
        if (
            Path(resolved.destination) != self.destination
            or Path(resolved.release_directory) != expected_release
        ):
            raise CodexDependencyInstallError(
                "plan", "install plan destinations do not match this installer"
            )
        previous_version = self._existing_version(self.destination)
        was_active = self._is_activated(expected_release)
        lock_target = self.standalone_root / "install"
        self.standalone_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(lock_target) as locked:
            if not locked:
                raise CodexDependencyInstallError(
                    "lock", "could not obtain the Codex standalone install lock"
                )
            active_executable = expected_release / "bin" / "codex"
            if active_executable.is_file() and self._receipt_matches(expected_release, resolved):
                try:
                    version = self._smoke(active_executable, resolved.version)
                except CodexDependencyInstallError:
                    quarantine = expected_release.with_name(
                        f"{expected_release.name}.failed.{uuid.uuid4().hex}"
                    )
                    os.replace(expected_release, quarantine)
                else:
                    backup_path = self._activate(expected_release, resolved.version)
                    return CodexInstallResult(
                        plan=resolved,
                        executable=str(self.destination),
                        smoke_version=version,
                        changed=not was_active or previous_version != version,
                        previous_version=previous_version,
                        backup_path=str(backup_path) if backup_path else None,
                    )
            elif expected_release.exists():
                quarantine = expected_release.with_name(
                    f"{expected_release.name}.unverified.{uuid.uuid4().hex}"
                )
                os.replace(expected_release, quarantine)

            staging_parent = self.standalone_root / ".staging"
            staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="codex-", dir=staging_parent) as temporary_dir:
                temporary = Path(temporary_dir)
                archive_path = temporary / resolved.asset_name
                if on_progress:
                    on_progress("downloading")
                archive_digest = self._download_file(resolved.asset_url, archive_path)
                if archive_digest != resolved.asset_sha256:
                    raise CodexDependencyInstallError(
                        "checksum", "archive SHA-256 did not match official release metadata"
                    )
                manifest = self._fetch_bytes(resolved.checksum_url)
                if hashlib.sha256(manifest).hexdigest() != resolved.checksum_sha256:
                    raise CodexDependencyInstallError(
                        "checksum", "checksum manifest SHA-256 did not match release metadata"
                    )
                manifest_digest = self._manifest_digest(manifest, resolved.asset_name)
                if manifest_digest != resolved.asset_sha256:
                    raise CodexDependencyInstallError(
                        "checksum", "release metadata and checksum manifest disagree"
                    )
                if on_progress:
                    on_progress("extracting")
                extracted = temporary / "candidate"
                extracted.mkdir(mode=0o700)
                package_root = self._extract(archive_path, extracted)
                candidate = package_root / "bin" / "codex"
                candidate.chmod(candidate.stat().st_mode | 0o100)
                if on_progress:
                    on_progress("verifying")
                version = self._smoke(candidate, resolved.version)
                self._write_receipt(package_root, resolved)
                expected_release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(package_root, expected_release)

            try:
                backup_path = self._activate(expected_release, resolved.version)
            except Exception:
                # Keep the verified release for a retry, but never claim it is active.
                raise
            if on_progress:
                on_progress("activated")
            return CodexInstallResult(
                plan=resolved,
                executable=str(self.destination),
                smoke_version=version,
                changed=not was_active or previous_version != version,
                previous_version=previous_version,
                backup_path=str(backup_path) if backup_path else None,
            )

    def _activate(self, release_directory: Path, expected_version: str) -> Path | None:
        current = self.standalone_root / "current"
        backups = self.standalone_root / "backups"
        current_backup = self._capture(current, backups)
        destination_backup = self._capture(self.destination, backups)
        backup_path = destination_backup.file_copy
        try:
            self._publish_symlink(current, str(release_directory))
            self._publish_symlink(self.destination, str(current / "bin" / "codex"))
            self._smoke(self.destination, expected_version)
        except Exception as exc:
            try:
                self._restore(self.destination, destination_backup)
                self._restore(current, current_backup)
            except OSError as rollback_exc:
                raise CodexDependencyInstallError(
                    "rollback",
                    f"activation failed ({exc}) and rollback failed ({rollback_exc})",
                ) from rollback_exc
            if isinstance(exc, CodexDependencyInstallError):
                raise
            raise CodexDependencyInstallError("activate", str(exc)) from exc
        return backup_path


__all__ = [
    "CODEX_GITHUB_METADATA_URL",
    "CODEX_MINIMUM_VERSION",
    "CODEX_OFFICIAL_INSTALL_COMMAND",
    "CODEX_RELEASE_CHANNEL",
    "CODEX_RELEASE_METADATA_URL",
    "CodexDependencyInstallError",
    "CodexDependencyInstaller",
    "CodexInstallPlan",
    "CodexInstallResult",
    "managed_codex_executable",
]
