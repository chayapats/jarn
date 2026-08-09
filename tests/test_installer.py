"""End-to-end tests for the one-command POSIX shell installer."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="install.sh targets macOS/Linux/WSL, not native Windows",
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_installer(
    tmp_path: Path,
    *,
    release_binary: str | None,
    os_name: str = "linux",
    arch: str = "x86_64",
    uv_bin: Path | None = None,
    uv_install_script: Path | None = None,
    valid_checksum: bool = True,
) -> subprocess.CompletedProcess[str]:
    release = tmp_path / "releases" / "download" / "v0.11.0"
    release.mkdir(parents=True)
    if release_binary is not None:
        asset = release / "jarn-linux-x86_64"
        _write_executable(asset, release_binary)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        if not valid_checksum:
            digest = "0" * 64
        (release / "checksums.txt").write_text(
            f"{digest}  {asset.name}\n", encoding="utf-8"
        )

    home = tmp_path / "home"
    home.mkdir()
    install_dir = home / ".local" / "bin"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "JARN_ARCH": arch,
            "JARN_GITHUB_BASE": f"file://{tmp_path}",
            "JARN_GITHUB_REPO": ".",
            "JARN_INSTALL_DIR": str(install_dir),
            "JARN_OS": os_name,
            "JARN_RUN_SETUP": "never",
            "JARN_VERSION": "0.11.0",
        }
    )
    if uv_bin is not None:
        env["JARN_UV_BIN"] = str(uv_bin)
    if uv_install_script is not None:
        env["JARN_UV_INSTALL_URL"] = f"file://{uv_install_script}"
        env["PATH"] = "/usr/bin:/bin"

    return subprocess.run(
        ["sh", str(INSTALLER)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_verifies_and_installs_release_binary(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary="#!/bin/sh\necho 'jarn 0.11.0'\n",
    )

    assert result.returncode == 0, result.stderr
    installed = tmp_path / "home" / ".local" / "bin" / "jarn"
    assert installed.is_file()
    assert subprocess.check_output([installed, "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )
    assert "SHA-256 verified" in result.stdout
    assert "Installed via binary" in result.stdout


def test_installer_falls_back_when_binary_cannot_start(tmp_path: Path) -> None:
    uv_log = tmp_path / "uv.log"
    uv_bin = tmp_path / "fake-uv"
    _write_executable(
        uv_bin,
        """#!/bin/sh
printf '%s\n' "$*" > "$JARN_TEST_UV_LOG"
mkdir -p "$UV_TOOL_BIN_DIR"
printf '%s\n' '#!/bin/sh' "echo 'jarn 0.11.0'" > "$UV_TOOL_BIN_DIR/jarn"
chmod 755 "$UV_TOOL_BIN_DIR/jarn"
""",
    )
    old_log = os.environ.get("JARN_TEST_UV_LOG")
    os.environ["JARN_TEST_UV_LOG"] = str(uv_log)
    try:
        result = _run_installer(
            tmp_path,
            release_binary=(
                "#!/bin/sh\n"
                "echo 'GLIBC_2.38 not found' >&2\n"
                "exit 1\n"
            ),
            uv_bin=uv_bin,
        )
    finally:
        if old_log is None:
            os.environ.pop("JARN_TEST_UV_LOG", None)
        else:
            os.environ["JARN_TEST_UV_LOG"] = old_log

    assert result.returncode == 0, result.stderr
    assert "portable Python fallback" in result.stdout
    assert "Installed via python" in result.stdout
    assert "--python 3.12 --managed-python --force jarn==0.11.0" in uv_log.read_text()


def test_installer_rejects_checksum_mismatch_without_fallback(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary="#!/bin/sh\necho 'jarn 0.11.0'\n",
        valid_checksum=False,
    )

    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stderr
    assert not (tmp_path / "home" / ".local" / "bin" / "jarn").exists()


def test_installer_uses_python_for_unsupported_platform(tmp_path: Path) -> None:
    uv_bin = tmp_path / "fake-uv"
    _write_executable(
        uv_bin,
        """#!/bin/sh
mkdir -p "$UV_TOOL_BIN_DIR"
printf '%s\n' '#!/bin/sh' "echo 'jarn 0.11.0'" > "$UV_TOOL_BIN_DIR/jarn"
chmod 755 "$UV_TOOL_BIN_DIR/jarn"
""",
    )
    result = _run_installer(
        tmp_path,
        release_binary=None,
        os_name="darwin",
        arch="x86_64",
        uv_bin=uv_bin,
    )

    assert result.returncode == 0, result.stderr
    assert "Detected darwin/x86_64" in result.stdout
    assert "Installed via python" in result.stdout


def test_installer_bootstraps_uv_when_it_is_missing(tmp_path: Path) -> None:
    uv_installer = tmp_path / "install-uv.sh"
    _write_executable(
        uv_installer,
        """#!/bin/sh
mkdir -p "$HOME/.local/bin"
printf '%s\n' '#!/bin/sh' 'mkdir -p "$UV_TOOL_BIN_DIR"' \\
  'printf "#!/bin/sh\\necho jarn 0.11.0\\n" > "$UV_TOOL_BIN_DIR/jarn"' \\
  'chmod 755 "$UV_TOOL_BIN_DIR/jarn"' > "$HOME/.local/bin/uv"
chmod 755 "$HOME/.local/bin/uv"
""",
    )
    result = _run_installer(
        tmp_path,
        release_binary=None,
        os_name="darwin",
        arch="x86_64",
        uv_install_script=uv_installer,
    )

    assert result.returncode == 0, result.stderr
    assert "Installing uv" in result.stdout
    assert "Installed via python" in result.stdout
