from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from jarn.codex_dependency import (
    CODEX_GITHUB_METADATA_URL,
    CODEX_RELEASE_METADATA_URL,
    CodexDependencyInstaller,
    CodexDependencyInstallError,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="standalone installer targets macOS/Linux")

VERSION = "0.147.0"
TARGET = "x86_64-unknown-linux-musl"
ASSET_NAME = f"codex-package-{TARGET}.tar.gz"
ASSET_URL = f"https://releases.openai.com/codex/releases/{VERSION}/{ASSET_NAME}"
CHECKSUM_URL = f"https://releases.openai.com/codex/releases/{VERSION}/codex-package_SHA256SUMS"


def _archive(*, traversal: bool = False) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        script = f"#!/bin/sh\necho 'codex-cli {VERSION}'\n".encode()
        executable = tarfile.TarInfo("bin/codex")
        executable.mode = 0o755
        executable.size = len(script)
        archive.addfile(executable, io.BytesIO(script))
        metadata = b'{"package":"codex"}\n'
        info = tarfile.TarInfo("codex-package.json")
        info.mode = 0o644
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
        if traversal:
            bad = b"unsafe"
            info = tarfile.TarInfo("../outside")
            info.size = len(bad)
            archive.addfile(info, io.BytesIO(bad))
    return payload.getvalue()


def _release_fixture(
    archive: bytes,
    *,
    advertised_archive_digest: str | None = None,
) -> tuple[dict[str, object], bytes]:
    archive_digest = advertised_archive_digest or hashlib.sha256(archive).hexdigest()
    checksums = f"{archive_digest}  {ASSET_NAME}\n".encode()
    metadata: dict[str, object] = {
        "tag_name": f"rust-v{VERSION}",
        "assets": [
            {
                "name": ASSET_NAME,
                "digest": f"sha256:{archive_digest}",
                "browser_download_url": ASSET_URL,
            },
            {
                "name": "codex-package_SHA256SUMS",
                "digest": f"sha256:{hashlib.sha256(checksums).hexdigest()}",
                "browser_download_url": CHECKSUM_URL,
            },
        ],
    }
    return metadata, checksums


def _installer(
    tmp_path: Path,
    archive: bytes,
    *,
    metadata: dict[str, object] | None = None,
    checksums: bytes | None = None,
    fetch_failure: bool = False,
    smoke=None,
) -> CodexDependencyInstaller:
    fixture_metadata, fixture_checksums = _release_fixture(archive)
    fixture_metadata = metadata or fixture_metadata
    fixture_checksums = checksums or fixture_checksums

    def fetch(url: str) -> bytes:
        if fetch_failure and url == CODEX_RELEASE_METADATA_URL:
            raise OSError("primary unavailable")
        if url in (CODEX_RELEASE_METADATA_URL, CODEX_GITHUB_METADATA_URL):
            return json.dumps(fixture_metadata).encode()
        if url == CHECKSUM_URL:
            return fixture_checksums
        raise AssertionError(f"unexpected fetch: {url}")

    def download(url: str, destination: Path) -> str:
        assert url == ASSET_URL
        destination.write_bytes(archive)
        return hashlib.sha256(archive).hexdigest()

    return CodexDependencyInstaller(
        home=tmp_path / "home",
        codex_home=tmp_path / "codex-home",
        system="linux",
        machine="x86_64",
        fetch_bytes=fetch,
        download_file=download,
        smoke=smoke,
    )


def test_plan_discloses_official_source_version_destination_and_sha256(tmp_path):
    installer = _installer(tmp_path, _archive())

    payload = installer.resolve_plan().to_dict()

    assert payload["name"] == "OpenAI Codex CLI"
    assert payload["purpose"] == "ChatGPT subscription authentication and model access"
    assert payload["version"] == VERSION
    assert payload["channel"] == "latest"
    assert payload["source"] == "OpenAI Releases"
    assert payload["destination"] == str(tmp_path / "home" / ".local" / "bin" / "codex")
    assert payload["verification"]["algorithm"] == "sha256"
    assert payload["verification"]["signature"] == "not_published_for_standalone_package"


def test_plan_falls_back_to_official_openai_github_release(tmp_path):
    installer = _installer(tmp_path, _archive(), fetch_failure=True)

    plan = installer.resolve_plan()

    assert plan.metadata_url == CODEX_GITHUB_METADATA_URL
    assert plan.source == "openai/codex GitHub release"


def test_missing_dependency_installs_without_node_and_activates_atomically(tmp_path):
    installer = _installer(tmp_path, _archive())
    plan = installer.resolve_plan()

    result = installer.install(plan)

    destination = Path(result.executable)
    assert result.changed is True
    assert result.smoke_version == VERSION
    assert destination.is_symlink()
    completed = subprocess.run(
        [str(destination), "--version"], check=True, capture_output=True, text=True
    )
    assert completed.stdout.strip() == f"codex-cli {VERSION}"
    assert (Path(plan.release_directory) / ".jarn-install.json").is_file()
    assert (tmp_path / "codex-home" / "packages" / "standalone" / "current").is_symlink()


def test_verified_same_version_rerun_is_idempotent(tmp_path):
    installer = _installer(tmp_path, _archive())
    plan = installer.resolve_plan()
    first = installer.install(plan)

    second = installer.install(plan)

    assert first.changed is True
    assert second.changed is False
    assert second.smoke_version == VERSION


def test_checksum_mismatch_never_activates_candidate(tmp_path):
    archive = _archive()
    metadata, checksums = _release_fixture(archive, advertised_archive_digest="0" * 64)
    installer = _installer(
        tmp_path,
        archive,
        metadata=metadata,
        checksums=checksums,
    )
    plan = installer.resolve_plan()

    with pytest.raises(CodexDependencyInstallError, match="archive SHA-256"):
        installer.install(plan)

    assert not Path(plan.destination).exists()
    assert not (tmp_path / "codex-home" / "packages" / "standalone" / "current").exists()


def test_archive_path_traversal_is_rejected_before_activation(tmp_path):
    installer = _installer(tmp_path, _archive(traversal=True))
    plan = installer.resolve_plan()

    with pytest.raises(CodexDependencyInstallError, match="unsafe path"):
        installer.install(plan)

    assert not (tmp_path / "outside").exists()
    assert not Path(plan.destination).exists()


def test_activation_smoke_failure_restores_previous_executable(tmp_path):
    calls = 0

    def smoke(_path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("activated candidate could not start")
        return f"codex-cli {VERSION}"

    installer = _installer(tmp_path, _archive(), smoke=smoke)
    installer.destination.parent.mkdir(parents=True)
    installer.destination.write_text("#!/bin/sh\necho 'codex-cli 0.1.0'\n", encoding="utf-8")
    installer.destination.chmod(0o755)
    plan = installer.resolve_plan()

    with pytest.raises(CodexDependencyInstallError, match="activated candidate"):
        installer.install(plan)

    assert installer.destination.read_text(encoding="utf-8").endswith("codex-cli 0.1.0'\n")
    assert not installer.destination.is_symlink()


def test_download_failure_preserves_existing_outdated_executable(tmp_path):
    installer = _installer(tmp_path, _archive())
    installer.destination.parent.mkdir(parents=True)
    installer.destination.write_text("#!/bin/sh\necho 'codex-cli 0.1.0'\n", encoding="utf-8")
    installer.destination.chmod(0o755)
    plan = installer.resolve_plan()

    def fail_download(_url: str, _destination: Path) -> str:
        raise OSError("network interrupted")

    installer._download_file_override = fail_download
    with pytest.raises(CodexDependencyInstallError, match="network interrupted"):
        installer.install(plan)

    assert installer.destination.read_text(encoding="utf-8").endswith("codex-cli 0.1.0'\n")
    assert not installer.destination.is_symlink()
