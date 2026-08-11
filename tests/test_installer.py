"""End-to-end contract tests for the one-command POSIX installer.

The suite uses real POSIX shells/filesystem activation with local ``file://``
release fixtures.  Package/network processes are faked only at their external
boundary, so PATH precedence, profile loading, atomic replacement, and rollback
are exercised by the actual installer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = Path(os.environ.get("JARN_INSTALLER_UNDER_TEST", ROOT / "install.sh")).resolve()

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="install.sh explicitly redirects native Windows users to WSL2",
)


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _receipt_doctor_guard(version: str, *, indent: str = "    ") -> str:
    """Return a fixture branch that independently reads the emitted receipt."""

    return f"""{indent}if [ "${{JARN_INSTALL_RECEIPT_VALIDATION:-0}}" = 1 ]; then
{indent}  printf '{{"jarn": {{"install": {{"metadata_present": true, "metadata_source": "canonical-install-record", "metadata_path": "%s", "version": "{version}", "active_matches_record": true}}}}}}\\n' "$JARN_STATE_DIR/install.json"
{indent}  exit 1
{indent}fi
"""


def _jarn_script(version: str, *, marker: str = "") -> str:
    marker_line = f"echo {marker!r}\n" if marker else ""
    return f"""#!/bin/sh
case "${{1:-}}" in
  --version) echo 'jarn {version}' ;;
  --help) echo 'usage: jarn [command]' ;;
  doctor)
{_receipt_doctor_guard(version)}    exit 0
    ;;
  *) {marker_line}exit 0 ;;
esac
"""


def _write_fake_uv(
    path: Path,
    *,
    jarn_version: str = "0.11.0",
    noise: str = "",
    fail_install: bool = False,
) -> None:
    fail_line = "echo 'simulated uv failure' >&2; exit 42" if fail_install else ""
    candidate_literal = shlex.quote(_jarn_script(jarn_version))
    _write_executable(
        path,
        f"""#!/bin/sh
if [ "${{1:-}}" = "--version" ]; then
  echo 'uv 0.12.3'
  exit 0
fi
printf '%s\n' "$*" > "${{JARN_TEST_UV_LOG:-/dev/null}}"
{f"echo {noise!r}" if noise else ":"}
{fail_line}
mkdir -p "$UV_TOOL_BIN_DIR"
printf '%s' {candidate_literal} > "$UV_TOOL_BIN_DIR/jarn"
chmod 755 "$UV_TOOL_BIN_DIR/jarn"
""",
    )


def _release_asset_name(os_name: str, arch: str) -> str | None:
    key = (os_name.lower(), arch)
    return {
        ("linux", "x86_64"): "jarn-linux-x86_64",
        ("linux", "arm64"): "jarn-linux-arm64",
        ("darwin", "arm64"): "jarn-macos-arm64",
    }.get(key)


def _run_installer(
    tmp_path: Path,
    *,
    release_binary: str | None,
    version: str = "0.11.0",
    requested_version: str | None = None,
    os_name: str = "linux",
    arch: str = "x86_64",
    distro_id: str = "ubuntu",
    distro_version: str = "22.04",
    libc_name: str = "glibc",
    libc_version: str = "2.35",
    uv_bin: Path | None = None,
    uv_install_script: Path | None = None,
    valid_checksum: bool = True,
    include_install_dir_on_path: bool = True,
    path_prefixes: tuple[Path, ...] = (),
    args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    tag = f"v{version}"
    release = tmp_path / "releases" / "download" / tag
    release.mkdir(parents=True, exist_ok=True)
    asset_name = _release_asset_name(os_name, arch)
    if release_binary is not None and asset_name is not None:
        asset = release / asset_name
        _write_executable(asset, release_binary)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        if not valid_checksum:
            digest = "0" * 64
        (release / "checksums.txt").write_text(f"{digest}  {asset.name}\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    install_dir = home / ".local" / "bin"
    state_dir = home / ".local" / "state" / "jarn"
    path_parts = [str(path) for path in path_prefixes]
    if include_install_dir_on_path:
        path_parts.append(str(install_dir))
    path_parts.extend(["/usr/bin", "/bin"])

    env = os.environ.copy()
    for key in ("CI", "SSH_CONNECTION", "SSH_TTY", "WSL_DISTRO_NAME"):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "PATH": os.pathsep.join(path_parts),
            "SHELL": "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh",
            "JARN_ARCH": arch,
            "JARN_AVAILABLE_DISK_KB": "1048576",
            "JARN_DISTRO_ID": distro_id,
            "JARN_DISTRO_VERSION": distro_version,
            "JARN_GITHUB_BASE": f"file://{tmp_path}",
            "JARN_GITHUB_REPO": ".",
            # The suite-wide fixture points JARN_HOME at its own safety
            # directory.  Installer fixtures model a real user's HOME instead,
            # so keep config/auth/readiness state in the same isolated home as
            # the executable and install receipt.
            "JARN_HOME": str(home / ".jarn"),
            "JARN_INSTALL_DIR": str(install_dir),
            "JARN_LIBC_NAME": libc_name,
            "JARN_LIBC_VERSION": libc_version,
            "JARN_OS": os_name,
            "JARN_RUN_SETUP": "never",
            "JARN_STATE_DIR": str(state_dir),
            "JARN_VERSION": requested_version or version,
        }
    )
    if os_name.lower() == "darwin":
        env["JARN_DISTRO_VERSION"] = distro_version if distro_version != "22.04" else "13.5"
        env["JARN_LIBC_NAME"] = "none"
        env["JARN_LIBC_VERSION"] = ""
    if uv_bin is not None:
        env["JARN_UV_BIN"] = str(uv_bin)
    if uv_install_script is not None:
        env["JARN_UV_INSTALL_URL"] = f"file://{uv_install_script}"
        env["JARN_UV_INSTALL_SHA256"] = hashlib.sha256(
            uv_install_script.read_bytes()
        ).hexdigest()
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["sh", str(INSTALLER), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _installed(tmp_path: Path) -> Path:
    return _home(tmp_path) / ".local" / "bin" / "jarn"


def _manifest(tmp_path: Path) -> dict[str, object]:
    path = _home(tmp_path) / ".local" / "state" / "jarn" / "install.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_prior_install(tmp_path: Path) -> dict[Path, bytes]:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))
    home = _home(tmp_path)
    profile = home / ".profile"
    bashrc = home / ".bashrc"
    profile.write_bytes(b"# exact prior profile\nexport KEEP_PROFILE=1\n")
    bashrc.write_bytes(b"# exact prior bashrc\nexport KEEP_BASHRC=1\n")
    manifest = home / ".local" / "state" / "jarn" / "install.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(
        (
            '{"schema_version":1,"version":"0.10.0","method":"binary",'
            f'"active_path":{json.dumps(str(old))}}}\n'
        ).encode()
    )
    legacy = old.parent / ".jarn-install-method"
    legacy.write_bytes(b"binary 0.10.0\n")
    return {
        old: old.read_bytes(),
        profile: profile.read_bytes(),
        bashrc: bashrc.read_bytes(),
        manifest: manifest.read_bytes(),
        legacy: legacy.read_bytes(),
    }


def _assert_prior_install_unchanged(files: dict[Path, bytes]) -> None:
    for path, expected in files.items():
        assert path.read_bytes() == expected, f"installer unexpectedly changed {path}"
    old = next(path for path in files if path.name == "jarn")
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"
    assert not list(old.parent.glob(".jarn.rollback.*"))
    assert not list(old.parent.glob(".jarn.failed.*"))


def _assert_install_error_anatomy(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 1
    for field in (
        "JARN-INSTALL-001: Installation did not complete.",
        "Cause:",
        "Component:",
        "Next:",
        "Log:",
    ):
        assert field in result.stderr
    assert "Done" not in result.stdout and "Ready" not in result.stdout


def _write_failing_curl(path: Path, *, exit_code: int) -> None:
    _write_executable(
        path,
        f"""#!/bin/sh
if [ "${{1:-}}" = "--version" ]; then
  echo 'curl 8.0 test fixture'
  echo 'Protocols: file http https'
  exit 0
fi
output=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ] && [ "$#" -ge 2 ]; then
    output=$2
    shift 2
  else
    shift
  fi
done
if [ -n "$output" ] && [ "$output" != /dev/null ]; then
  printf '%s' 'partial-transfer-bytes' > "$output"
fi
case {exit_code} in
  5) echo 'curl: (5) Could not resolve proxy' >&2 ;;
  6) echo 'curl: (6) Could not resolve host' >&2 ;;
  18) echo 'curl: (18) Transferred a partial file' >&2 ;;
  23) echo 'curl: (23) Failure writing output: No space left on device' >&2 ;;
  60) echo 'curl: (60) SSL certificate problem' >&2 ;;
esac
exit {exit_code}
""",
    )


def _write_sigkill_mv(path: Path, *, marker: Path, journal_phase: str) -> None:
    _write_executable(
        path,
        f"""#!/bin/sh
/bin/mv "$@" || exit $?
case "${{2:-}}" in
  */install.transaction)
    if grep -F 'phase={journal_phase}' "$2" >/dev/null 2>&1 && [ ! -e {str(marker)!r} ]; then
      : > {str(marker)!r}
      kill -KILL "$PPID"
    fi
    ;;
esac
""",
    )


def _canonical_install_one_liner() -> str:
    marker = "#   jarn_installer_tmp=$(mktemp"
    for line in INSTALLER.read_text(encoding="utf-8").splitlines():
        if line.startswith(marker):
            return line.removeprefix("#   ")
    raise AssertionError("canonical installer one-liner is missing")


def test_installer_help_documents_all_controls(tmp_path: Path) -> None:
    result = subprocess.run(
        ["sh", str(INSTALLER), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    for option in (
        "--version",
        "--channel",
        "--install-dir",
        "--method",
        "--no-setup",
        "--dry-run",
        "--yes",
        "--verbose",
        "--help",
    ):
        assert option in result.stdout
    assert "Exit status" in result.stdout
    assert "10" in result.stdout and "activation" in result.stdout.lower()
    assert "mktemp" in result.stdout and "curl -fsSL" in result.stdout
    assert "| sh" not in result.stdout


def test_canonical_curl_failure_never_executes_download_or_prints_success(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    marker = tmp_path / "download-was-executed"
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    _write_executable(fake_bin / "curl", "#!/bin/sh\nexit 56\n")
    _write_executable(
        fake_bin / "sh",
        '#!/bin/sh\n: > "$JARN_TEST_EXEC_MARKER"\necho Done\nexit 0\n',
    )
    env = os.environ.copy()
    env.update(
        {
            "JARN_TEST_EXEC_MARKER": str(marker),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "SHELL": "/bin/false",
            "TMPDIR": str(temp_dir),
        }
    )

    result = subprocess.run(
        ["/bin/sh", "-c", _canonical_install_one_liner()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 56
    assert not marker.exists(), "sh must not run when curl fails"
    assert "Done" not in result.stdout and "Ready" not in result.stdout
    assert list(temp_dir.iterdir()) == [], "the secure temporary file must be cleaned"


def test_dry_run_performs_preflight_without_persistent_changes(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--dry-run", "--method", "binary", "--no-setup"),
    )

    assert result.returncode == 0, result.stderr
    assert "Preflight:" in result.stdout
    assert "Dry run complete" in result.stdout
    assert not _installed(tmp_path).exists()
    assert not (_home(tmp_path) / ".local" / "state" / "jarn").exists()
    assert not (_home(tmp_path) / ".profile").exists()


def test_inventory_discovers_pip_user_pipx_and_default_uv_tool_commands(
    tmp_path: Path,
) -> None:
    manager_bin = tmp_path / "manager-bin"
    pip_user_base = tmp_path / "pip-user"
    pipx_bin = tmp_path / "pipx-bin"
    uv_bin = tmp_path / "uv-bin"
    for command in (
        pip_user_base / "bin" / "jarn",
        pipx_bin / "jarn",
        uv_bin / "jarn",
    ):
        _write_executable(command, _jarn_script("0.9.0"))
    _write_executable(
        manager_bin / "python3",
        f"#!/bin/sh\nprintf '%s\\n' {str(pip_user_base)!r}\n",
    )
    _write_executable(
        manager_bin / "pipx",
        f"#!/bin/sh\nprintf '%s\\n' {str(pipx_bin)!r}\n",
    )
    _write_executable(
        manager_bin / "uv",
        f"#!/bin/sh\nprintf '%s\\n' {str(uv_bin)!r}\n",
    )

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        include_install_dir_on_path=False,
        path_prefixes=(manager_bin,),
        args=("--dry-run", "--verbose", "--method", "binary", "--no-setup"),
    )

    assert result.returncode == 0, result.stderr
    assert f"pip-user: {pip_user_base / 'bin' / 'jarn'}" in result.stdout
    assert f"pipx: {pipx_bin / 'jarn'}" in result.stdout
    assert f"uv-tool: {uv_bin / 'jarn'}" in result.stdout


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="requires bash hash -p")
def test_inventory_reports_hashed_shell_resolution_even_when_not_on_path(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    home.mkdir(exist_ok=True)
    hashed = tmp_path / "hashed-only" / "jarn"
    _write_executable(hashed, _jarn_script("0.8.0"))
    (home / ".bashrc").write_text(f"hash -p {str(hashed)!r} jarn\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        include_install_dir_on_path=False,
        args=("--dry-run", "--verbose", "--method", "binary", "--no-setup"),
    )

    assert result.returncode == 0, result.stderr
    assert "shell aliases/functions/resolution:" in result.stdout
    assert str(hashed) in result.stdout


def test_binary_install_verifies_checksum_smokes_and_records_manifest(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    # A changed command deliberately requires a parent-shell refresh, even when
    # the child can already resolve it. This prevents hash/alias false success.
    assert result.returncode == 10, result.stderr
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )
    assert "SHA-256 verified" in result.stdout
    assert "Installed path verified" in result.stdout
    assert "Done" not in result.stdout
    assert "Ready" not in result.stdout
    assert "Activation required" in result.stderr

    manifest = _manifest(tmp_path)
    assert manifest["schema_version"] == 1
    assert manifest["version"] == "0.11.0"
    assert manifest["method"] == "binary"
    assert manifest["active_path"] == str(_installed(tmp_path))
    assert manifest["setup_status"] == "skipped"
    assert manifest["activation"]["status"] == "required"  # type: ignore[index]
    assert manifest["platform"]["libc_version"] == "2.35"  # type: ignore[index]
    assert (_home(tmp_path) / ".profile").is_file()
    assert (_home(tmp_path) / ".bashrc").is_file()


def test_linux_arm64_selects_and_activates_the_arm64_release_asset(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        arch="arm64",
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 10, result.stderr
    assert "jarn-linux-arm64" in result.stdout
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )
    manifest = _manifest(tmp_path)
    assert manifest["platform"]["architecture"] == "arm64"  # type: ignore[index]
    assert manifest["version"] == "0.11.0"
    assert manifest["method"] == "binary"
    assert manifest["active_path"] == str(_installed(tmp_path))


def test_same_version_rerun_is_idempotent_and_active(tmp_path: Path) -> None:
    first = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )
    assert first.returncode == 10, first.stderr

    second = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    assert second.returncode == 0, second.stderr
    assert "already healthy" in second.stdout
    assert "Ready" in second.stdout
    assert _manifest(tmp_path)["method"] == "binary"
    assert not list(_installed(tmp_path).parent.glob(".jarn.rollback.*"))
    assert (_home(tmp_path) / ".profile").read_text().count(">>> J.A.R.N. managed PATH >>>") == 1


def test_same_version_rerun_preserves_existing_rollback_candidate(tmp_path: Path) -> None:
    _seed_prior_install(tmp_path)
    first = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )
    assert first.returncode == 10, first.stderr
    first_previous = Path(str(_manifest(tmp_path)["previous_path"]))
    first_previous_bytes = first_previous.read_bytes()

    second = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    assert second.returncode == 0, second.stderr
    assert Path(str(_manifest(tmp_path)["previous_path"])) == first_previous
    assert first_previous.read_bytes() == first_previous_bytes
    assert len(list(_installed(tmp_path).parent.glob(".jarn.rollback.*"))) == 1


def test_glibc_binary_failure_uses_quiet_isolated_python_fallback(tmp_path: Path) -> None:
    uv_log = tmp_path / "uv.log"
    uv_bin = tmp_path / "fake-uv"
    _write_fake_uv(uv_bin, noise="PACKAGE-NOISE-SHOULD-BE-IN-LOG")

    result = _run_installer(
        tmp_path,
        release_binary=("#!/bin/sh\necho 'GLIBC_2.38 not found' >&2\nexit 1\n"),
        uv_bin=uv_bin,
        args=("--method", "auto", "--no-setup"),
        extra_env={"JARN_TEST_UV_LOG": str(uv_log)},
    )

    assert result.returncode == 10, result.stderr
    assert "release binary cannot run here: GLIBC_2.38 not found" in result.stderr
    assert "Using the isolated Python fallback" in result.stdout
    assert "PACKAGE-NOISE" not in result.stdout
    assert "PACKAGE-NOISE" not in result.stderr
    assert "--quiet --no-progress --python 3.12 --managed-python jarn==0.11.0" in (
        uv_log.read_text(encoding="utf-8")
    )
    assert _manifest(tmp_path)["method"] == "python"
    candidate_path = Path(str(_manifest(tmp_path)["candidate_path"]))
    assert candidate_path.is_file()
    assert "versions/python-0.11.0" in str(candidate_path)

    logs = list((_home(tmp_path) / ".local" / "state" / "jarn").glob("install-*.log"))
    assert len(logs) == 1
    assert "PACKAGE-NOISE-SHOULD-BE-IN-LOG" in logs[0].read_text(encoding="utf-8")


def test_python_fallback_preserves_uv_absolute_launcher_target(tmp_path: Path) -> None:
    """A real uv launcher must not dangle after fallback finalization."""

    uv_bin = tmp_path / "absolute-launcher-uv"
    candidate_literal = shlex.quote(_jarn_script("0.11.0"))
    _write_executable(
        uv_bin,
        f"""#!/bin/sh
if [ "${{1:-}}" = "--version" ]; then
  echo "uv 0.12.3"
  exit 0
fi
mkdir -p "$UV_TOOL_DIR/jarn/bin" "$UV_TOOL_BIN_DIR"
printf '%s' {candidate_literal} > "$UV_TOOL_DIR/jarn/bin/jarn"
chmod 755 "$UV_TOOL_DIR/jarn/bin/jarn"
ln -s "$UV_TOOL_DIR/jarn/bin/jarn" "$UV_TOOL_BIN_DIR/jarn"
""",
    )

    result = _run_installer(
        tmp_path,
        release_binary="#!/bin/sh\necho 'GLIBC_2.38 not found' >&2\nexit 1\n",
        uv_bin=uv_bin,
        args=("--method", "auto", "--no-setup"),
    )

    assert result.returncode == 10, result.stderr
    installed = _installed(tmp_path)
    assert installed.is_symlink()
    launcher = installed.resolve(strict=True)
    assert launcher.is_file()
    assert "/versions/python-0.11.0-" in str(launcher)
    assert subprocess.check_output([installed, "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )


def test_exact_npm_shadow_plus_glibc_regression_is_not_false_success(tmp_path: Path) -> None:
    old_prefix = tmp_path / "old-npm"
    old_jarn = old_prefix / "bin" / "jarn"
    _write_executable(
        old_jarn,
        "#!/bin/sh\necho 'old npm/PyInstaller: GLIBC_2.38 not found' >&2\nexit 1\n",
    )
    _write_executable(
        old_prefix / "bin" / "npm",
        f"#!/bin/sh\n[ \"${{1:-}}\" = prefix ] && printf '%s\\n' {str(old_prefix)!r}\n",
    )
    uv_bin = tmp_path / "fake-uv"
    uv_log = tmp_path / "uv.log"
    _write_fake_uv(uv_bin)

    result = _run_installer(
        tmp_path,
        release_binary=("#!/bin/sh\necho 'GLIBC_2.38 not found' >&2\nexit 1\n"),
        uv_bin=uv_bin,
        include_install_dir_on_path=False,
        path_prefixes=(old_prefix / "bin",),
        args=("--method", "auto", "--no-setup"),
        extra_env={"JARN_TEST_UV_LOG": str(uv_log)},
    )

    assert result.returncode == 10
    assert "Done" not in result.stdout
    assert "Ready" not in result.stdout
    assert "Activation required" in result.stderr
    assert f"current resolution: {old_jarn}" in result.stderr
    assert "other J.A.R.N. installations remain" in result.stderr
    assert old_jarn.exists(), "the old npm installation must never be deleted"
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(_home(tmp_path)),
            "PATH": f"{old_prefix / 'bin'}:/usr/bin:/bin",
        }
    )
    shell = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
    activated = subprocess.run(
        [shell, "-lic", "command -v jarn; jarn --version"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert activated.returncode == 0, activated.stderr
    assert str(_installed(tmp_path)) in activated.stdout
    assert "jarn 0.11.0" in activated.stdout


def test_checksum_mismatch_preserves_prior_executable(tmp_path: Path) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0", marker="old"))

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        valid_checksum=False,
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"
    assert not list(old.parent.glob(".jarn.rollback.*"))
    assert "Ready" not in result.stdout and "Done" not in result.stdout


@pytest.mark.parametrize(
    ("exit_code", "diagnostic"),
    [
        pytest.param(
            18,
            "download was interrupted or only partially transferred",
            id="interrupted-partial-download",
        ),
        pytest.param(
            23,
            "local download write failed (disk full or read-only filesystem)",
            id="mid-write-enospc",
        ),
        pytest.param(5, "proxy lookup failed", id="proxy-resolution"),
        pytest.param(6, "DNS lookup failed", id="dns-resolution"),
        pytest.param(
            60,
            "TLS handshake or certificate verification failed",
            id="tls-certificate",
        ),
    ],
)
def test_download_faults_are_distinct_and_preserve_all_prior_state(
    tmp_path: Path,
    exit_code: int,
    diagnostic: str,
) -> None:
    prior = _seed_prior_install(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    _write_failing_curl(fake_bin / "curl", exit_code=exit_code)
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        path_prefixes=(fake_bin,),
        args=("--method", "binary", "--no-setup"),
        extra_env={"TMPDIR": str(temp_dir)},
    )

    _assert_install_error_anatomy(result)
    assert diagnostic in result.stderr
    assert "retry exactly after network recovery" in result.stderr
    _assert_prior_install_unchanged(prior)
    assert not list(temp_dir.glob("jarn-install.*")), "partial download staging must be cleaned"


@pytest.mark.parametrize(
    ("target", "label"),
    [("install", "installation directory"), ("state", "state directory")],
)
def test_read_only_target_preflight_is_deterministic_and_non_mutating(
    tmp_path: Path,
    target: str,
    label: str,
) -> None:
    prior = _seed_prior_install(tmp_path)
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
        extra_env={
            "JARN_TEST_READ_ONLY_TARGET": target,
            "TMPDIR": str(temp_dir),
        },
    )

    _assert_install_error_anatomy(result)
    assert f"{label} is not writable" in result.stderr
    assert "injected read-only target" in result.stderr
    assert "Downloading" not in result.stdout
    _assert_prior_install_unchanged(prior)
    assert not list(temp_dir.glob("jarn-install.*"))


def test_upgrade_retains_prior_binary_as_rollback_candidate(tmp_path: Path) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 10, result.stderr
    manifest = _manifest(tmp_path)
    previous = Path(str(manifest["previous_path"]))
    assert previous.is_file()
    assert subprocess.check_output([previous, "--version"], text=True).strip() == "jarn 0.10.0"
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.11.0"
    assert "Rollback candidate retained" in result.stdout


def test_post_activation_smoke_failure_automatically_restores_prior(tmp_path: Path) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))
    profile = _home(tmp_path) / ".profile"
    bashrc = _home(tmp_path) / ".bashrc"
    original_profile = b"# original login profile\nexport KEEP_LOGIN=1\n"
    original_bashrc = b"# original interactive profile\nexport KEEP_INTERACTIVE=1\n"
    profile.write_bytes(original_profile)
    bashrc.write_bytes(original_bashrc)
    fails_only_at_active_path = """#!/bin/sh
case "${1:-}" in
  --version)
    case "$0" in
      */jarn) echo 'post-activation failure' >&2; exit 9 ;;
      *) echo 'jarn 0.11.0' ;;
    esac
    ;;
  --help) echo 'usage: jarn' ;;
  *) exit 0 ;;
esac
"""

    result = _run_installer(
        tmp_path,
        release_binary=fails_only_at_active_path,
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 1
    assert "prior executable was restored" in result.stderr
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"
    assert profile.read_bytes() == original_profile
    assert bashrc.read_bytes() == original_bashrc
    failed = list(old.parent.glob(".jarn.failed.0.11.0.*"))
    assert len(failed) == 1, "failed candidate should be retained for diagnosis"


def test_profile_activation_failure_restores_every_prior_profile_byte(
    tmp_path: Path,
) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))
    profile = _home(tmp_path) / ".profile"
    bashrc = _home(tmp_path) / ".bashrc"
    original_profile = b"# login bytes before install\r\nexport KEEP_LOGIN=1\n"
    original_bashrc = b"# interactive bytes before install\nexport KEEP_RC=1\n"
    profile.write_bytes(original_profile)
    bashrc.write_bytes(original_bashrc)

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
        extra_env={"JARN_TEST_FAIL_PROFILE_ACTIVATION_AT": "2"},
    )

    assert result.returncode == 1
    assert "injected shell profile activation failure" in result.stderr
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"
    assert profile.read_bytes() == original_profile
    assert bashrc.read_bytes() == original_bashrc
    assert not (_home(tmp_path) / ".local" / "state" / "jarn" / "install.json").exists()
    assert not list(_home(tmp_path).glob(".*.jarn-tmp.*"))


def test_partial_metadata_failure_restores_prior_executable_profiles_and_metadata(
    tmp_path: Path,
) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))
    profile = _home(tmp_path) / ".profile"
    bashrc = _home(tmp_path) / ".bashrc"
    original_profile = b"# exact original login profile\nexport KEEP=login\n"
    original_bashrc = b"# exact original interactive profile\nexport KEEP=interactive\n"
    profile.write_bytes(original_profile)
    bashrc.write_bytes(original_bashrc)

    state_dir = _home(tmp_path) / ".local" / "state" / "jarn"
    state_dir.mkdir(parents=True)
    manifest = state_dir / "install.json"
    legacy = old.parent / ".jarn-install-method"
    original_manifest = b'{"schema_version":1,"version":"0.10.0","method":"binary"}\n'
    original_legacy = b"binary 0.10.0\n"
    manifest.write_bytes(original_manifest)
    legacy.write_bytes(original_legacy)

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
        # Fail after the new manifest has replaced the old one, before the
        # companion method record is activated. This exercises partial writes.
        extra_env={"JARN_TEST_FAIL_METADATA_AT": "legacy"},
    )

    assert result.returncode == 1
    assert "could not persist install metadata" in result.stderr
    assert "prior executable was restored" in result.stderr
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"
    assert profile.read_bytes() == original_profile
    assert bashrc.read_bytes() == original_bashrc
    assert manifest.read_bytes() == original_manifest
    assert legacy.read_bytes() == original_legacy
    assert len(list(old.parent.glob(".jarn.failed.0.11.0.*"))) == 1
    assert not list(state_dir.glob("*.tmp.*"))


def test_metadata_failure_on_fresh_install_removes_command_and_created_profiles(
    tmp_path: Path,
) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
        extra_env={"JARN_TEST_FAIL_METADATA_AT": "legacy"},
    )

    assert result.returncode == 1
    assert "could not persist install metadata" in result.stderr
    assert not _installed(tmp_path).exists()
    assert not (_home(tmp_path) / ".profile").exists()
    assert not (_home(tmp_path) / ".bashrc").exists()
    assert not (_home(tmp_path) / ".local" / "state" / "jarn" / "install.json").exists()
    assert not (_installed(tmp_path).parent / ".jarn-install-method").exists()
    assert len(list(_installed(tmp_path).parent.glob(".jarn.failed.0.11.0.*"))) == 1


def test_candidate_receipt_rejection_restores_prior_install_atomically(tmp_path: Path) -> None:
    prior = _seed_prior_install(tmp_path)
    candidate = """#!/bin/sh
case "${1:-}" in
  --version) echo 'jarn 0.11.0' ;;
  --help) echo 'usage: jarn [command]' ;;
  doctor)
    if [ "${JARN_INSTALL_RECEIPT_VALIDATION:-0}" = 1 ]; then
      printf '%s\n' '{"jarn": {"install": {"metadata_present": false, "active_matches_record": false}}}'
      exit 1
    fi
    exit 0
    ;;
  *) exit 0 ;;
esac
"""

    result = _run_installer(
        tmp_path,
        release_binary=candidate,
        args=("--method", "binary", "--no-setup"),
    )

    _assert_install_error_anatomy(result)
    assert "candidate rejected or could not verify the emitted install receipt" in result.stderr
    for path, expected in prior.items():
        assert path.read_bytes() == expected
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.10.0"
    )
    assert len(list(_installed(tmp_path).parent.glob(".jarn.failed.0.11.0.*"))) == 1
    state_dir = _home(tmp_path) / ".local" / "state" / "jarn"
    assert not (state_dir / "install.transaction").exists()
    assert not (state_dir / "install.lock").exists()


def test_real_candidate_doctor_accepts_canonical_emitted_receipt(tmp_path: Path) -> None:
    candidate = f"""#!/bin/sh
case "${{1:-}}" in
  --version) echo 'jarn 0.11.0' ;;
  --help) echo 'usage: jarn [command]' ;;
  doctor) exec {shlex.quote(sys.executable)} -m jarn "$@" ;;
  *) exit 0 ;;
esac
"""

    result = _run_installer(
        tmp_path,
        release_binary=candidate,
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 10, result.stderr
    receipt = _manifest(tmp_path)
    assert receipt["active_path"] == str(_installed(tmp_path))
    assert receipt["candidate_path"] == str(_installed(tmp_path))


def test_live_installer_lock_fails_closed_and_preserves_owner_and_prior_install(
    tmp_path: Path,
) -> None:
    prior = _seed_prior_install(tmp_path)
    lock = _home(tmp_path) / ".local" / "state" / "jarn" / "install.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    _assert_install_error_anatomy(result)
    assert f"another install/update is active with pid {os.getpid()}" in result.stderr
    assert (lock / "pid").read_text(encoding="utf-8") == f"{os.getpid()}\n"
    _assert_prior_install_unchanged(prior)


def test_stale_installer_lock_is_quarantined_then_install_retries(tmp_path: Path) -> None:
    _seed_prior_install(tmp_path)
    state_dir = _home(tmp_path) / ".local" / "state" / "jarn"
    lock = state_dir / "install.lock"
    lock.mkdir()
    (lock / "pid").write_text("99999999\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 10, result.stderr
    assert "Recovered stale pid 99999999 installer lock" in result.stderr
    recovered = list(state_dir.glob("install.lock.recovered.*"))
    assert len(recovered) == 1
    assert (recovered[0] / "pid").read_text(encoding="utf-8") == "99999999\n"
    assert not lock.exists()
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )


@pytest.mark.parametrize(
    "lock_kind",
    ["regular-file", "missing-pid", "invalid-pid", "pid-symlink", "lock-symlink"],
)
def test_malformed_and_symlink_locks_are_quarantined_without_following_links(
    tmp_path: Path,
    lock_kind: str,
) -> None:
    state_dir = _home(tmp_path) / ".local" / "state" / "jarn"
    state_dir.mkdir(parents=True)
    lock = state_dir / "install.lock"
    sentinel = tmp_path / "external-lock-target"
    sentinel.write_bytes(b"external-bytes")

    if lock_kind == "regular-file":
        lock.write_text("not-a-directory\n", encoding="utf-8")
    elif lock_kind == "lock-symlink":
        external_dir = tmp_path / "external-lock-dir"
        external_dir.mkdir()
        (external_dir / "sentinel").write_bytes(b"external-directory-bytes")
        lock.symlink_to(external_dir, target_is_directory=True)
    else:
        lock.mkdir()
        if lock_kind == "invalid-pid":
            (lock / "pid").write_text("not-a-pid\n", encoding="utf-8")
        elif lock_kind == "pid-symlink":
            (lock / "pid").symlink_to(sentinel)

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 10, result.stderr
    reason = "symbolic-link" if lock_kind == "lock-symlink" else "malformed"
    assert f"Recovered {reason} installer lock" in result.stderr
    assert len(list(state_dir.glob("install.lock.recovered.*"))) == 1
    assert sentinel.read_bytes() == b"external-bytes"
    if lock_kind == "lock-symlink":
        assert (tmp_path / "external-lock-dir" / "sentinel").read_bytes() == (
            b"external-directory-bytes"
        )
    assert not lock.exists()


def test_sigkill_before_first_activation_rename_preserves_exact_prior_chain(
    tmp_path: Path,
) -> None:
    _seed_prior_install(tmp_path)
    older_rollback = _installed(tmp_path).parent / ".jarn.rollback.0.9.0.existing"
    _write_executable(older_rollback, _jarn_script("0.9.0"))
    kill_marker = tmp_path / "killed-prepared-once"
    fake_bin = tmp_path / "fake-bin"
    _write_sigkill_mv(
        fake_bin / "mv",
        marker=kill_marker,
        journal_phase="prepared",
    )

    interrupted = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        path_prefixes=(fake_bin,),
        args=("--method", "binary", "--no-setup"),
    )

    assert interrupted.returncode == -signal.SIGKILL
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.10.0"
    )
    assert subprocess.check_output([older_rollback, "--version"], text=True).strip() == (
        "jarn 0.9.0"
    )
    state_dir = _home(tmp_path) / ".local" / "state" / "jarn"
    journal = state_dir / "install.transaction"
    assert "phase=prepared" in journal.read_text(encoding="utf-8")
    assert len(list(_installed(tmp_path).parent.glob(".jarn.activate.*"))) == 1

    recovered = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        path_prefixes=(fake_bin,),
        args=("--method", "binary", "--no-setup"),
    )

    assert recovered.returncode == 10, recovered.stderr
    assert "interrupted before retaining the prior executable" in recovered.stderr
    receipt = _manifest(tmp_path)
    latest_previous = Path(str(receipt["previous_path"]))
    assert subprocess.check_output([latest_previous, "--version"], text=True).strip() == (
        "jarn 0.10.0"
    )
    assert subprocess.check_output([older_rollback, "--version"], text=True).strip() == (
        "jarn 0.9.0"
    )
    assert not journal.exists()
    assert len(list(state_dir.glob("install.lock.recovered.*"))) == 1
    assert list(_installed(tmp_path).parent.glob(".jarn.failed.interrupted.*"))


def test_sigkill_during_activation_is_reconciled_on_exact_rerun(tmp_path: Path) -> None:
    _seed_prior_install(tmp_path)
    kill_marker = tmp_path / "killed-once"
    fake_bin = tmp_path / "fake-bin"
    _write_sigkill_mv(
        fake_bin / "mv",
        marker=kill_marker,
        journal_phase="activated",
    )

    interrupted = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        path_prefixes=(fake_bin,),
        args=("--method", "binary", "--no-setup"),
    )

    assert interrupted.returncode == -signal.SIGKILL
    assert kill_marker.is_file()
    state_dir = _home(tmp_path) / ".local" / "state" / "jarn"
    assert (state_dir / "install.lock").is_dir()
    assert (state_dir / "install.transaction").is_file()
    assert "phase=activated" in (state_dir / "install.transaction").read_text(encoding="utf-8")
    rollback = list(_installed(tmp_path).parent.glob(".jarn.rollback.0.10.0.*"))
    assert len(rollback) == 1
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )

    recovered = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        path_prefixes=(fake_bin,),
        args=("--method", "binary", "--no-setup"),
    )

    assert recovered.returncode == 0, recovered.stderr
    assert "Recovered stale pid" in recovered.stderr
    assert "Recovered verified J.A.R.N. 0.11.0 from interrupted activation" in recovered.stderr
    receipt = _manifest(tmp_path)
    assert Path(str(receipt["previous_path"])) == rollback[0]
    assert subprocess.check_output([rollback[0], "--version"], text=True).strip() == "jarn 0.10.0"
    assert not (state_dir / "install.lock").exists()
    assert not (state_dir / "install.transaction").exists()
    assert len(list(state_dir.glob("install.lock.recovered.*"))) == 1
    assert (_home(tmp_path) / ".profile").read_text().count(">>> J.A.R.N. managed PATH >>>") == 1


def test_signal_after_backup_restores_prior_executable(tmp_path: Path) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))
    fake_bin = tmp_path / "fake-bin"
    _write_executable(
        fake_bin / "mv",
        """#!/bin/sh
/bin/mv "$@" || exit $?
case "${2:-}" in
  *.jarn.rollback.*) kill -TERM "$PPID" ;;
esac
""",
    )

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        path_prefixes=(fake_bin,),
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 143
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"
    assert not (_home(tmp_path) / ".local" / "state" / "jarn" / "install.json").exists()


def test_shell_alias_collision_blocks_completion_and_rolls_back(tmp_path: Path) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))
    alias_target = tmp_path / "alias-jarn"
    _write_executable(alias_target, _jarn_script("9.9.9"))
    home = _home(tmp_path)
    home.mkdir(exist_ok=True)
    original_bashrc = f"alias jarn={str(alias_target)!r}\n".encode()
    (home / ".bashrc").write_bytes(original_bashrc)

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 1
    assert "interactive-shell resolution" in result.stderr
    assert "shell-resolution verification failed" in result.stderr
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"
    assert (home / ".bashrc").read_bytes() == original_bashrc
    assert not (home / ".profile").exists()
    assert not (_home(tmp_path) / ".local" / "state" / "jarn" / "install.json").exists()


@pytest.mark.parametrize(
    ("os_name", "distro_id", "distro_version", "libc_name", "message"),
    [
        ("MSYS_NT-10.0", "", "", "none", "native Windows"),
        ("linux", "alpine", "3.20", "musl", "unsupported Linux target"),
        ("linux", "fedora", "42", "glibc", "unsupported Linux target"),
        ("linux", "ubuntu", "18.04", "glibc", "unsupported Linux target"),
    ],
)
def test_unsupported_platform_fails_before_download(
    tmp_path: Path,
    os_name: str,
    distro_id: str,
    distro_version: str,
    libc_name: str,
    message: str,
) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=None,
        os_name=os_name,
        distro_id=distro_id,
        distro_version=distro_version,
        libc_name=libc_name,
        libc_version="",
        args=("--no-setup",),
    )

    assert result.returncode == 1
    assert message in result.stderr
    assert "Downloading" not in result.stdout
    assert not (_home(tmp_path) / ".local" / "state" / "jarn").exists()


def test_glibc_older_than_ubuntu_2004_is_rejected_preflight(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=None,
        libc_version="2.30",
        args=("--no-setup",),
    )

    assert result.returncode == 1
    assert "supported minimum 2.31" in result.stderr
    assert "Downloading" not in result.stdout


def test_insufficient_disk_fails_before_state_or_download(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--no-setup",),
        extra_env={"JARN_AVAILABLE_DISK_KB": "1024"},
    )

    assert result.returncode == 1
    assert "insufficient disk space" in result.stderr
    assert "Downloading" not in result.stdout
    assert not (_home(tmp_path) / ".local" / "state" / "jarn").exists()


def test_unavailable_binary_preserves_prior_installation(tmp_path: Path) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))

    result = _run_installer(
        tmp_path,
        release_binary=None,
        args=("--method", "binary", "--no-setup"),
    )

    assert result.returncode == 1
    assert "release asset" in result.stderr
    assert "retry exactly after network recovery" in result.stderr
    assert "--version '0.11.0' --method 'binary' --no-setup" in result.stderr
    assert "prior J.A.R.N. remains unchanged" in result.stderr
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"


def test_python_install_failure_preserves_prior_installation(tmp_path: Path) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))
    uv_bin = tmp_path / "fake-uv"
    _write_fake_uv(uv_bin, fail_install=True)

    result = _run_installer(
        tmp_path,
        release_binary=None,
        uv_bin=uv_bin,
        args=("--method", "python", "--no-setup"),
    )

    assert result.returncode == 1
    assert "isolated Python installation failed" in result.stderr
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"


def test_bootstrapped_uv_is_disclosed_and_owned_in_manifest(tmp_path: Path) -> None:
    uv_installer = tmp_path / "install-uv.sh"
    uv_body = f"""#!/bin/sh
if [ "${{1:-}}" = "--version" ]; then
  echo "uv 0.12.3"
  exit 0
fi
mkdir -p "$UV_TOOL_BIN_DIR"
printf '%s' {shlex.quote(_jarn_script("0.11.0"))} > "$UV_TOOL_BIN_DIR/jarn"
chmod 755 "$UV_TOOL_BIN_DIR/jarn"
"""
    _write_executable(
        uv_installer,
        f"""#!/bin/sh
mkdir -p "$HOME/.local/bin"
printf '%s' {shlex.quote(uv_body)} > "$HOME/.local/bin/uv"
chmod 755 "$HOME/.local/bin/uv"
""",
    )

    result = _run_installer(
        tmp_path,
        release_binary=None,
        os_name="darwin",
        arch="x86_64",
        uv_install_script=uv_installer,
        args=("--method", "python", "--no-setup"),
    )

    assert result.returncode == 10, result.stderr
    assert "External dependency: uv 0.12.3" in result.stdout
    assert f"source: file://{uv_installer}" in result.stdout
    dependency = _manifest(tmp_path)["dependency"]
    assert dependency["uv_owned_by_jarn"] is True  # type: ignore[index]
    assert str(dependency["uv_path"]).endswith("/.local/bin/uv")  # type: ignore[index]


def test_uv_installer_checksum_mismatch_never_executes_or_replaces_prior(
    tmp_path: Path,
) -> None:
    old = _installed(tmp_path)
    _write_executable(old, _jarn_script("0.10.0"))
    marker = _home(tmp_path) / "uv-installer-ran"
    uv_installer = tmp_path / "untrusted-uv-installer.sh"
    _write_executable(
        uv_installer,
        f"#!/bin/sh\nprintf '%s\\n' ran > {shlex.quote(str(marker))}\n",
    )

    result = _run_installer(
        tmp_path,
        release_binary=None,
        os_name="darwin",
        arch="x86_64",
        uv_install_script=uv_installer,
        args=("--method", "python", "--no-setup"),
        extra_env={"JARN_UV_INSTALL_SHA256": "0" * 64},
    )

    assert result.returncode == 1
    assert "uv installer SHA-256 mismatch" in result.stderr
    assert "JARN-INSTALL-001" in result.stderr
    assert not marker.exists()
    assert subprocess.check_output([old, "--version"], text=True).strip() == "jarn 0.10.0"


def test_verbose_mode_streams_dependency_details(tmp_path: Path) -> None:
    uv_bin = tmp_path / "fake-uv"
    _write_fake_uv(uv_bin, noise="VERBOSE-PACKAGE-DETAIL")

    result = _run_installer(
        tmp_path,
        release_binary=None,
        os_name="darwin",
        arch="x86_64",
        uv_bin=uv_bin,
        args=("--method", "python", "--no-setup", "--verbose"),
    )

    assert result.returncode == 10, result.stderr
    assert "VERBOSE-PACKAGE-DETAIL" in result.stdout


def test_command_line_install_dir_overrides_environment(tmp_path: Path) -> None:
    custom = _home(tmp_path) / "custom tools" / "bin"
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=(
            "--method",
            "binary",
            "--install-dir",
            str(custom),
            "--version",
            "0.11.0",
            "--channel",
            "stable",
            "--yes",
            "--no-setup",
        ),
    )

    assert result.returncode == 10, result.stderr
    assert (custom / "jarn").is_file()
    assert f"into {custom / 'jarn'}" in result.stdout
    assert str(custom) in (_home(tmp_path) / ".profile").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("env_name", "unsafe_path", "label"),
    [
        ("JARN_INSTALL_DIR", "relative/../escaped-bin", "installation directory"),
        ("JARN_STATE_DIR", "/tmp/jarn-safe/../escaped-state", "state directory"),
    ],
)
def test_parent_directory_components_are_rejected_before_mutation(
    tmp_path: Path,
    env_name: str,
    unsafe_path: str,
    label: str,
) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
        extra_env={env_name: unsafe_path},
    )

    _assert_install_error_anatomy(result)
    assert f"{label} must not contain a parent-directory (..) component" in result.stderr
    assert "Downloading" not in result.stdout
    assert not _installed(tmp_path).exists()
    assert not (_home(tmp_path) / ".local" / "state" / "jarn").exists()


def test_safe_relative_install_and_state_paths_are_normalized_to_absolute(
    tmp_path: Path,
) -> None:
    install_path = ROOT / "relative-bin"
    state_path = ROOT / "relative-state"

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        include_install_dir_on_path=False,
        args=("--dry-run", "--verbose", "--method", "binary", "--no-setup"),
        extra_env={
            "JARN_INSTALL_DIR": "./relative-bin",
            "JARN_STATE_DIR": "./relative-state",
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"into {install_path / 'jarn'}" in result.stdout
    assert f"state directory: {state_path}" in result.stdout
    assert not install_path.exists()
    assert not state_path.exists()


@pytest.mark.parametrize("symlink_kind", ["install", "state", "versions", "parent"])
def test_managed_path_symlinks_are_rejected_without_touching_target(
    tmp_path: Path,
    symlink_kind: str,
) -> None:
    home = _home(tmp_path)
    home.mkdir(exist_ok=True)
    real_target = tmp_path / f"real-{symlink_kind}"
    real_target.mkdir()
    sentinel = real_target / "sentinel"
    sentinel.write_bytes(b"do-not-touch")
    local = home / ".local"

    if symlink_kind == "parent":
        local.symlink_to(real_target, target_is_directory=True)
    else:
        local.mkdir()
        if symlink_kind == "install":
            (local / "bin").symlink_to(real_target, target_is_directory=True)
        else:
            state_parent = local / "state"
            state_parent.mkdir()
            if symlink_kind == "state":
                (state_parent / "jarn").symlink_to(real_target, target_is_directory=True)
            else:
                state_dir = state_parent / "jarn"
                state_dir.mkdir()
                (state_dir / "versions").symlink_to(real_target, target_is_directory=True)

    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary", "--no-setup"),
    )

    _assert_install_error_anatomy(result)
    assert "traverses a symbolic link" in result.stderr
    assert "Downloading" not in result.stdout
    assert sentinel.read_bytes() == b"do-not-touch"
    assert not (real_target / "jarn").exists()
    assert not (real_target / "install.json").exists()


def test_unknown_option_fails_without_mutation(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--definitely-not-real",),
    )

    assert result.returncode == 1
    assert "unknown installer option" in result.stderr
    assert not _installed(tmp_path).exists()
    assert not (_home(tmp_path) / ".local" / "state" / "jarn").exists()


@pytest.mark.parametrize(
    ("context_env", "expected_context"),
    [({}, "headless"), ({"SSH_CONNECTION": "fixture"}, "ssh")],
    ids=("non-interactive", "ssh-headless"),
)
def test_headless_auto_setup_returns_incomplete_with_manifest_preserved(
    tmp_path: Path,
    context_env: dict[str, str],
    expected_context: str,
) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary"),
        extra_env={"JARN_RUN_SETUP": "auto", **context_env},
    )

    assert result.returncode == 20
    assert "Setup incomplete" in result.stderr
    assert "no success status was emitted" in result.stderr
    assert "Done" not in result.stdout and "Ready" not in result.stdout
    assert f"context {expected_context}" in result.stdout
    assert _installed(tmp_path).is_file()
    manifest = _manifest(tmp_path)
    assert manifest["setup_status"] == "required"
    assert manifest["activation"]["status"] == "required"  # type: ignore[index]


@pytest.mark.parametrize(
    ("config_text", "readiness_failure"),
    [
        ("config_version: [broken\n", "malformed configuration"),
        (
            "config_version: 3\nproviders:\n  main:\n    api_key: ${MISSING_API_KEY}\n",
            "missing credential",
        ),
        (
            "config_version: 3\nprofiles:\n  default:\n    model: retired-model\n",
            "unavailable saved model",
        ),
    ],
    ids=("malformed-yaml", "missing-secret", "unavailable-model"),
)
def test_existing_but_unready_config_never_produces_ready(
    tmp_path: Path,
    config_text: str,
    readiness_failure: str,
) -> None:
    config = _home(tmp_path) / ".jarn" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(config_text, encoding="utf-8")
    doctor_log = tmp_path / "doctor-failure.txt"
    candidate = f"""#!/bin/sh
case "${{1:-}}" in
  --version) echo 'jarn 0.11.0' ;;
  --help) echo 'usage: jarn [command]' ;;
  doctor)
{_receipt_doctor_guard("0.11.0")}
    printf '%s\n' {readiness_failure!r} > {str(doctor_log)!r}
    exit 2
    ;;
  *) exit 0 ;;
esac
"""

    result = _run_installer(
        tmp_path,
        release_binary=candidate,
        args=("--method", "binary"),
        extra_env={"JARN_RUN_SETUP": "auto"},
    )

    assert result.returncode == 20
    assert "existing config/auth/model route is not ready" in result.stderr
    assert "doctor --json" in result.stderr
    assert "Ready" not in result.stdout
    assert doctor_log.read_text(encoding="utf-8").strip() == readiness_failure
    assert config.read_text(encoding="utf-8") == config_text
    assert _manifest(tmp_path)["setup_status"] == "failed"


def test_existing_ready_config_is_verified_not_assumed(tmp_path: Path) -> None:
    config = _home(tmp_path) / ".jarn" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("config_version: 3\n", encoding="utf-8")
    doctor_log = tmp_path / "doctor-ran.txt"
    candidate = f"""#!/bin/sh
case "${{1:-}}" in
  --version) echo 'jarn 0.11.0' ;;
  --help) echo 'usage: jarn [command]' ;;
  doctor)
{_receipt_doctor_guard("0.11.0")}
    [ "${{2:-}}" = "--json" ] || exit 51
    command -v jarn > {str(doctor_log)!r}
    [ "$(command -v jarn)" = "$0" ] || exit 52
    ;;
  *) exit 0 ;;
esac
"""

    result = _run_installer(
        tmp_path,
        release_binary=candidate,
        args=("--method", "binary"),
        extra_env={"JARN_RUN_SETUP": "auto"},
    )

    assert result.returncode in {0, 10}, result.stderr
    assert doctor_log.read_text(encoding="utf-8").strip() == str(_installed(tmp_path))
    assert _manifest(tmp_path)["setup_status"] == "existing"
    assert "Setup incomplete" not in result.stderr


def test_interactive_setup_resolves_just_activated_command_before_parent_shell(
    tmp_path: Path,
) -> None:
    """The one-command flow must not let an old parent PATH invalidate setup."""

    old_bin = tmp_path / "old-npm" / "jarn"
    _write_executable(old_bin, _jarn_script("0.10.0"))
    setup_log = tmp_path / "setup-resolution.txt"
    candidate = f"""#!/bin/sh
case "${{1:-}}" in
  --version) echo 'jarn 0.11.0' ;;
  --help) echo 'usage: jarn [command]' ;;
  doctor)
{_receipt_doctor_guard("0.11.0")}    exit 0
    ;;
  setup)
    resolved=$(command -v jarn) || exit 31
    printf '%s\n' "$resolved" > {str(setup_log)!r}
    [ "$resolved" = "$0" ] || exit 32
    grep -F '"active_path": "'"$0"'"' "$JARN_STATE_DIR/install.json" >/dev/null || exit 33
    setup_tmp="$JARN_STATE_DIR/install.json.setup.$$"
    sed 's/"setup_status": "pending"/"setup_status": "complete"/; s/"setup_status":"pending"/"setup_status":"complete"/' "$JARN_STATE_DIR/install.json" > "$setup_tmp" || exit 34
    mv "$setup_tmp" "$JARN_STATE_DIR/install.json" || exit 35
    ;;
  *) exit 0 ;;
esac
"""

    result = _run_installer(
        tmp_path,
        release_binary=candidate,
        include_install_dir_on_path=False,
        path_prefixes=(old_bin.parent,),
        args=("--method", "binary"),
        extra_env={
            "JARN_RUN_SETUP": "always",
            "JARN_TEST_FORCE_INTERACTIVE_SETUP": "1",
        },
    )

    assert result.returncode == 10, result.stderr
    assert setup_log.read_text(encoding="utf-8").strip() == str(_installed(tmp_path))
    manifest = _manifest(tmp_path)
    assert manifest["setup_status"] == "complete"
    assert "Setup incomplete" not in result.stderr


def test_zero_exit_without_verified_setup_status_is_not_success(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        release_binary=_jarn_script("0.11.0"),
        args=("--method", "binary"),
        extra_env={
            "JARN_RUN_SETUP": "always",
            "JARN_TEST_FORCE_INTERACTIVE_SETUP": "1",
        },
    )

    assert result.returncode == 20
    assert "child exited zero" in result.stderr
    assert _manifest(tmp_path)["setup_status"] == "pending"
    assert _installed(tmp_path).is_file()


def test_setup_failure_keeps_new_install_and_exact_prior_config(tmp_path: Path) -> None:
    config = _home(tmp_path) / ".jarn" / "config.yaml"
    config.parent.mkdir(parents=True)
    original = b"config_version: 3\ndefault_profile: anthropic\n"
    config.write_bytes(original)
    candidate = f"""#!/bin/sh
case "${{1:-}}" in
  --version) echo 'jarn 0.11.0' ;;
  --help) echo 'usage: jarn [command]' ;;
  doctor)
{_receipt_doctor_guard("0.11.0")}    exit 0
    ;;
  setup) exit 42 ;;
  *) exit 0 ;;
esac
"""

    result = _run_installer(
        tmp_path,
        release_binary=candidate,
        args=("--method", "binary"),
        extra_env={
            "JARN_RUN_SETUP": "always",
            "JARN_TEST_FORCE_INTERACTIVE_SETUP": "1",
        },
    )

    assert result.returncode == 20
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )
    assert config.read_bytes() == original
    assert _manifest(tmp_path)["setup_status"] == "pending"


def test_signal_after_setup_commit_keeps_install_and_config_consistent(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config = home / ".jarn" / "config.yaml"
    _write_executable(_installed(tmp_path), _jarn_script("0.10.0"))
    config.parent.mkdir(parents=True)
    config.write_text("config_version: 3\ndefault_profile: anthropic\n", encoding="utf-8")
    candidate = f"""#!/bin/sh
case "${{1:-}}" in
  --version) echo 'jarn 0.11.0' ;;
  --help) echo 'usage: jarn [command]' ;;
  doctor)
{_receipt_doctor_guard("0.11.0")}    exit 0
    ;;
  setup)
    mkdir -p {str(config.parent)!r}
    printf '%s\n' 'config_version: 3' 'default_profile: codex_subscription' > {str(config)!r}
    setup_tmp="$JARN_STATE_DIR/install.json.setup.$$"
    sed 's/"setup_status": "pending"/"setup_status": "complete"/; s/"setup_status":"pending"/"setup_status":"complete"/' "$JARN_STATE_DIR/install.json" > "$setup_tmp" || exit 41
    mv "$setup_tmp" "$JARN_STATE_DIR/install.json" || exit 42
    kill -TERM "$PPID"
    ;;
  *) exit 0 ;;
esac
"""

    result = _run_installer(
        tmp_path,
        release_binary=candidate,
        args=("--method", "binary"),
        extra_env={
            "JARN_RUN_SETUP": "always",
            "JARN_TEST_FORCE_INTERACTIVE_SETUP": "1",
        },
    )

    assert result.returncode == 143
    assert subprocess.check_output([_installed(tmp_path), "--version"], text=True).strip() == (
        "jarn 0.11.0"
    )
    assert config.read_text(encoding="utf-8").startswith("config_version: 3")
    manifest = _manifest(tmp_path)
    assert manifest["setup_status"] == "complete"
    previous = Path(str(manifest["previous_path"]))
    assert previous.is_file()
    assert subprocess.check_output([previous, "--version"], text=True).strip() == "jarn 0.10.0"
