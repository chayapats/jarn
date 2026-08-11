"""GA update/install-record/rollback contract tests."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
from functools import partial
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_V0_CONFIG = """\
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


def _release(
    version: str,
    *,
    prerelease: bool = False,
    draft: bool = False,
    name: str | None = None,
    body: str | None = None,
    published_at: str | None = None,
) -> dict:
    result = {"tag_name": f"v{version}", "prerelease": prerelease, "draft": draft}
    if name is not None:
        result["name"] = name
    if body is not None:
        result["body"] = body
    if published_at is not None:
        result["published_at"] = published_at
    return result


def _record(
    active: Path,
    previous: Path | None,
    *,
    version: str = "2.0.0",
    method: str = "binary",
    state_dir: Path | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "version": version,
        "method": method,
        "channel": "stable",
        "active_path": str(active),
        "candidate_path": str(active),
        "previous_path": str(previous) if previous else None,
        "state_dir": str(state_dir or active.parent.parent / "state"),
        "platform": {"os": "linux", "libc_version": "2.35"},
        "dependency": {"uv_owned_by_jarn": False},
        "activation": {"status": "active"},
        "setup_status": "complete",
        "installed_at": "2026-08-09T00:00:00Z",
    }


def _write_record(
    manifest: Path,
    active: Path,
    previous: Path | None = None,
    *,
    version: str,
    method: str = "binary",
) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            _record(
                active,
                previous,
                version=version,
                method=method,
                state_dir=manifest.parent,
            )
        ),
        encoding="utf-8",
    )


def _command(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = --version ]; then echo 'jarn {version}'; exit 0; fi\n"
        'if [ "$1" = --help ]; then echo help; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_install_record_rejects_relative_action_path(tmp_path: Path) -> None:
    from jarn.install_state import InstallStateError, load_install_record

    manifest = tmp_path / "install.json"
    value = _record(tmp_path / "jarn", None)
    value["active_path"] = "relative/jarn"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(InstallStateError, match="unsafe relative active_path"):
        load_install_record(manifest)


def test_actionable_record_rejects_absolute_unowned_file(tmp_path: Path) -> None:
    from jarn.install_state import InstallStateError, load_actionable_install_record

    state = tmp_path / "state"
    state.mkdir()
    unrelated = tmp_path / "documents" / "jarn"
    unrelated.parent.mkdir()
    unrelated.write_text("valuable user file", encoding="utf-8")
    manifest = state / "install.json"
    value = _record(tmp_path / "bin" / "jarn", None)
    value["active_path"] = str(unrelated)
    manifest.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(InstallStateError, match="outside the proven installer-owned"):
        load_actionable_install_record(manifest)
    assert unrelated.read_text(encoding="utf-8") == "valuable user file"


def test_actionable_record_rejects_unmanaged_rollback_name(tmp_path: Path) -> None:
    from jarn.install_state import InstallStateError, load_actionable_install_record

    active = tmp_path / "bin" / "jarn"
    _command(active, "2.0.0")
    unrelated = active.parent / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    manifest = state / "install.json"
    manifest.write_text(json.dumps(_record(active, unrelated)), encoding="utf-8")

    with pytest.raises(InstallStateError, match="rollback path"):
        load_actionable_install_record(manifest)
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_check_update_stable_excludes_prerelease() -> None:
    from jarn.update import check_for_update

    result = check_for_update(
        channel="stable",
        current_version="1.0.0",
        _fetch=lambda: [
            _release("2.1.0b1", prerelease=True),
            _release("2.0.0"),
            _release("9.0.0", draft=True),
        ],
    )
    assert result.latest_version == "2.0.0"
    assert result.update_available is True


def test_check_update_beta_includes_prerelease() -> None:
    from jarn.update import check_for_update

    result = check_for_update(
        channel="beta",
        current_version="2.0.0",
        _fetch=lambda: [_release("2.1.0b1", prerelease=True), _release("2.0.0")],
    )
    assert result.latest_version == "2.1.0b1"


def test_update_current_is_idempotent_and_does_not_download(capsys, tmp_path: Path) -> None:
    from jarn.update import run_update
    from jarn.version import __version__

    called: list[str] = []
    active = tmp_path / "bin" / "jarn"
    manifest = tmp_path / "state" / "install.json"
    _command(active, __version__)
    _write_record(manifest, active, version=__version__)

    assert (
        run_update(
            _fetch=lambda: [_release(__version__)],
            _download=lambda version, path: called.append(version) or "unused",
            _manifest_path=manifest,
        )
        == 0
    )
    assert called == []
    assert "already current" in capsys.readouterr().out


def test_downloaded_installer_requires_matching_release_checksum(tmp_path: Path) -> None:
    from jarn.update import _download_installer

    script = b"#!/bin/sh\necho safe\n"
    digest = hashlib.sha256(script).hexdigest()
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return script if url.endswith("/install.sh") else f"{digest}  install.sh\n".encode()

    target = tmp_path / "install.sh"
    _download_installer("9.9.9", target, _fetch=fetch)

    assert target.read_bytes() == script
    assert target.stat().st_mode & 0o777 == 0o700
    assert seen == [
        "https://raw.githubusercontent.com/chayapats/jarn/v9.9.9/install.sh",
        "https://github.com/chayapats/jarn/releases/download/v9.9.9/checksums.txt",
    ]


def test_ci_canary_sources_use_credential_free_loopback_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarn import update

    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("JARN_UPDATE_CANARY_MODE", "1")
    monkeypatch.setenv("JARN_UPDATE_CANARY_RELEASES_API", "http://127.0.0.1:8765/releases.json")
    monkeypatch.setenv("JARN_UPDATE_CANARY_RAW_BASE", "http://127.0.0.1:8765/raw")
    monkeypatch.setenv("JARN_UPDATE_CANARY_DOWNLOAD_BASE", "http://127.0.0.1:8765/download")
    script = b"#!/bin/sh\necho canary\n"
    digest = hashlib.sha256(script).hexdigest()
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return script if url.endswith("/install.sh") else f"{digest}  install.sh\n".encode()

    target = tmp_path / "install.sh"
    source = update._download_installer("9.9.9", target, _fetch=fetch)

    assert source == "http://127.0.0.1:8765/raw/v9.9.9/install.sh"
    assert seen == [
        "http://127.0.0.1:8765/raw/v9.9.9/install.sh",
        "http://127.0.0.1:8765/download/v9.9.9/checksums.txt",
    ]
    assert target.read_bytes() == script


def test_ci_canary_release_catalog_uses_validated_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarn import update

    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("JARN_UPDATE_CANARY_MODE", "1")
    monkeypatch.setenv("JARN_UPDATE_CANARY_RELEASES_API", "http://localhost:8765/releases.json")
    monkeypatch.setenv("JARN_UPDATE_CANARY_RAW_BASE", "http://localhost:8765/raw")
    monkeypatch.setenv("JARN_UPDATE_CANARY_DOWNLOAD_BASE", "http://localhost:8765/download")
    seen: list[tuple[str, float]] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int = -1) -> bytes:
            return b'[{"tag_name":"v9.9.9","draft":false,"prerelease":false}]'

    def urlopen(request, *, timeout):
        seen.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(update.urllib.request, "urlopen", urlopen)

    assert update._default_fetch_releases() == [
        {"tag_name": "v9.9.9", "draft": False, "prerelease": False}
    ]
    assert seen == [("http://localhost:8765/releases.json", 10.0)]


def test_release_catalog_read_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarn import update

    seen_sizes: list[int] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size: int) -> bytes:
            seen_sizes.append(size)
            return b"[" + (b" " * size)

    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(ValueError, match="release response exceeded"):
        update._default_fetch_releases()
    assert seen_sizes == [512 * 1024 + 1]


def test_canary_source_overrides_fail_closed_outside_explicit_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarn.update import UpdateError, _download_installer

    monkeypatch.setenv("JARN_UPDATE_CANARY_RELEASES_API", "http://127.0.0.1:8765/releases.json")
    monkeypatch.setenv("JARN_UPDATE_CANARY_RAW_BASE", "http://127.0.0.1:8765/raw")
    monkeypatch.setenv("JARN_UPDATE_CANARY_DOWNLOAD_BASE", "http://127.0.0.1:8765/download")
    monkeypatch.delenv("JARN_UPDATE_CANARY_MODE", raising=False)
    monkeypatch.delenv("CI", raising=False)

    with pytest.raises(UpdateError, match="explicit CI canary mode"):
        _download_installer("9.9.9", tmp_path / "install.sh", _fetch=lambda _: b"")


def test_canary_source_overrides_fail_closed_when_only_partially_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarn.update import UpdateError, _download_installer

    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("JARN_UPDATE_CANARY_MODE", "1")
    monkeypatch.setenv("JARN_UPDATE_CANARY_RAW_BASE", "http://127.0.0.1:8765/raw")
    monkeypatch.delenv("JARN_UPDATE_CANARY_RELEASES_API", raising=False)
    monkeypatch.delenv("JARN_UPDATE_CANARY_DOWNLOAD_BASE", raising=False)

    with pytest.raises(UpdateError, match="must be supplied together"):
        _download_installer("9.9.9", tmp_path / "install.sh", _fetch=lambda _: b"")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("JARN_UPDATE_CANARY_RAW_BASE", "http://example.test/raw"),
        ("JARN_UPDATE_CANARY_RAW_BASE", "https://token:secret@example.test/raw"),
        ("JARN_UPDATE_CANARY_DOWNLOAD_BASE", "file:///tmp/draft"),
        ("JARN_UPDATE_CANARY_RELEASES_API", "https://example.test/releases?token=secret"),
    ],
)
def test_canary_source_validation_rejects_unsafe_or_secret_bearing_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    from jarn.update import UpdateError, _download_installer

    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("JARN_UPDATE_CANARY_MODE", "1")
    monkeypatch.setenv("JARN_UPDATE_CANARY_RELEASES_API", "https://fixtures.example/releases")
    monkeypatch.setenv("JARN_UPDATE_CANARY_RAW_BASE", "https://fixtures.example/raw")
    monkeypatch.setenv("JARN_UPDATE_CANARY_DOWNLOAD_BASE", "https://fixtures.example/download")
    monkeypatch.setenv(name, value)

    with pytest.raises(UpdateError) as error:
        _download_installer("9.9.9", tmp_path / "install.sh", _fetch=lambda _: b"")
    assert "credential-free HTTPS or a loopback" in str(error.value)
    assert "token" not in str(error.value)
    assert "secret" not in str(error.value)


def test_downloaded_installer_checksum_mismatch_is_never_published(tmp_path: Path) -> None:
    from jarn.update import UpdateError, _download_installer

    target = tmp_path / "install.sh"

    def fetch(url: str) -> bytes:
        if url.endswith("/install.sh"):
            return b"#!/bin/sh\necho compromised\n"
        return ("0" * 64 + "  install.sh\n").encode()

    with pytest.raises(UpdateError, match="SHA-256 mismatch"):
        _download_installer("9.9.9", target, _fetch=fetch)
    assert not target.exists()


def test_update_delegates_to_canonical_installer_and_preserves_exit_10(tmp_path, capsys) -> None:
    from jarn.update import run_update

    seen: list[list[str]] = []
    active = tmp_path / "bin" / "jarn"
    manifest = tmp_path / "state" / "install.json"
    _command(active, "0.11.0")
    _write_record(manifest, active, version="0.11.0")

    def download(version: str, path: Path) -> str:
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        return f"https://example.test/v{version}/install.sh"

    def runner(argv, **kwargs):
        seen.append(argv)
        assert kwargs == {"check": False}
        return subprocess.CompletedProcess(argv, 10)

    code = run_update(
        _fetch=lambda: [_release("99.0.0")],
        _download=download,
        _runner=runner,
        _manifest_path=manifest,
        _config_path=tmp_path / "config.yaml",
    )
    assert code == 10
    argv = seen[0]
    assert argv[1].endswith("install.sh")
    assert argv[argv.index("--version") + 1] == "99.0.0"
    assert argv[argv.index("--method") + 1] == "binary"
    assert "--no-setup" in argv and "--yes" in argv
    assert "remains available" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="release canary installer requires POSIX")
def test_actual_update_command_uses_loopback_canary_and_retains_historical_command(
    tmp_path: Path,
) -> None:
    version = "0.11.0"
    origin = tmp_path / "origin"
    raw_dir = origin / "raw" / f"v{version}"
    download_dir = origin / "download" / f"v{version}"
    asset_dir = origin / "github" / "example" / "jarn" / "releases" / "download" / f"v{version}"
    for directory in (raw_dir, download_dir, asset_dir):
        directory.mkdir(parents=True)
    installer = (REPO / "install.sh").read_bytes()
    binary = (
        "#!/bin/sh\n"
        'case "${1:-}" in\n'
        f"  --version) echo 'jarn {version}' ;;\n"
        "  --help) echo 'usage: jarn [command]' ;;\n"
        "  doctor)\n"
        '    if [ "${JARN_INSTALL_RECEIPT_VALIDATION:-0}" = 1 ]; then\n'
        "      printf '{\"jarn\": {\"install\": {\"metadata_present\": true, "
        "\"metadata_source\": \"canonical-install-record\", "
        "\"metadata_path\": \"%s\", "
        f"\"version\": \"{version}\", \"active_matches_record\": true}}}}\\n' "
        '"$JARN_STATE_DIR/install.json"\n'
        "      exit 1\n"
        "    fi\n"
        "    exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    ).encode()
    checksums = (
        f"{hashlib.sha256(installer).hexdigest()}  install.sh\n"
        f"{hashlib.sha256(binary).hexdigest()}  jarn-linux-x86_64\n"
    ).encode()
    (raw_dir / "install.sh").write_bytes(installer)
    (download_dir / "checksums.txt").write_bytes(checksums)
    (asset_dir / "checksums.txt").write_bytes(checksums)
    (asset_dir / "jarn-linux-x86_64").write_bytes(binary)
    (origin / "releases.json").write_text(
        json.dumps([{"tag_name": f"v{version}", "draft": False, "prerelease": False}]),
        encoding="utf-8",
    )

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

    try:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(origin))
        )
    except PermissionError:
        pytest.skip("execution sandbox does not allow a loopback listener")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    home = tmp_path / "home"
    active = home / ".local" / "bin" / "jarn"
    _command(active, "0.10.0")
    manifest_path = home / ".local" / "state" / "jarn" / "install.json"
    _write_record(manifest_path, active, version="0.10.0")
    env = os.environ.copy()
    env.update(
        {
            "CI": "true",
            "HOME": str(home),
            "PATH": f"{active.parent}:/usr/bin:/bin",
            "PYTHONPATH": str(REPO / "src"),
            "SHELL": "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh",
            "JARN_ARCH": "x86_64",
            "JARN_AVAILABLE_DISK_KB": "1048576",
            "JARN_DISTRO_ID": "ubuntu",
            "JARN_DISTRO_VERSION": "22.04",
            "JARN_GITHUB_BASE": f"{base}/github",
            "JARN_GITHUB_REPO": "example/jarn",
            "JARN_LIBC_NAME": "glibc",
            "JARN_LIBC_VERSION": "2.35",
            "JARN_OS": "linux",
            "JARN_RUN_SETUP": "never",
            "JARN_STATE_DIR": str(manifest_path.parent),
            "JARN_UPDATE_CANARY_MODE": "1",
            "JARN_UPDATE_CANARY_RELEASES_API": f"{base}/releases.json",
            "JARN_UPDATE_CANARY_RAW_BASE": f"{base}/raw",
            "JARN_UPDATE_CANARY_DOWNLOAD_BASE": f"{base}/download",
        }
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "jarn", "update", "--version", version],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 10, result.stderr
    assert f"Updating from verified source: {base}/raw/v{version}/install.sh" in result.stdout
    assert subprocess.check_output([active, "--version"], text=True).strip() == f"jarn {version}"
    manifest = json.loads((home / ".local" / "state" / "jarn" / "install.json").read_text())
    previous = Path(manifest["previous_path"])
    assert subprocess.check_output([previous, "--version"], text=True).strip() == "jarn 0.10.0"


def test_update_json_captures_installer_noise(tmp_path: Path, capsys) -> None:
    from jarn.update import run_update

    active = tmp_path / "bin" / "jarn"
    manifest = tmp_path / "state" / "install.json"
    _command(active, "0.11.0")
    _write_record(manifest, active, version="0.11.0")

    def download(_version: str, path: Path) -> str:
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        return "https://example.test/install.sh"

    def runner(argv, **kwargs):
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(argv, 0, stdout="noisy stage output", stderr="")

    assert (
        run_update(
            as_json=True,
            _fetch=lambda: [_release("99.0.0")],
            _download=download,
            _runner=runner,
            _manifest_path=manifest,
            _config_path=tmp_path / "config.yaml",
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["installerExitCode"] == 0


@pytest.mark.parametrize(
    ("method", "kind", "command"),
    [
        ("uv", "uv", "uv tool install --force jarn==99.0.0"),
        ("pipx", "pipx", "pipx install --force jarn==99.0.0"),
        ("pip-user", "pip", "python3 -m pip install --upgrade jarn==99.0.0"),
        ("npm", "npm", "npm install --global jarn-cli@99.0.0"),
        ("homebrew", "homebrew", "brew upgrade jarn"),
    ],
)
def test_update_refuses_to_switch_shared_manager_ownership(
    tmp_path: Path,
    capsys,
    method: str,
    kind: str,
    command: str,
) -> None:
    from jarn.update import run_update

    active = tmp_path / "bin" / "jarn"
    manifest = tmp_path / "state" / "install.json"
    _command(active, "0.11.0")
    _write_record(manifest, active, version="0.11.0", method=method)
    mutations: list[str] = []

    result = run_update(
        as_json=True,
        _fetch=lambda: [_release("99.0.0", body="No breaking changes declared.")],
        _download=lambda *_args: mutations.append("download") or "unused",
        _runner=lambda *_args, **_kwargs: (
            mutations.append("run") or subprocess.CompletedProcess([], 0)
        ),
        _manifest_path=manifest,
        _config_path=tmp_path / "config.yaml",
    )

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["changed"] is False
    assert payload["preview"]["ownership"]["kind"] == kind
    assert payload["preview"]["ownership"]["managerCommand"] == command
    assert "refused ownership change" in payload["preview"]["action"]
    assert mutations == []


@pytest.mark.parametrize("method", ["binary", "python"])
def test_curl_managed_update_reuses_exact_installer_method(
    tmp_path: Path,
    method: str,
) -> None:
    from jarn.update import run_update

    active = tmp_path / "bin" / "jarn"
    manifest = tmp_path / "state" / "install.json"
    _command(active, "0.11.0")
    _write_record(manifest, active, version="0.11.0", method=method)
    seen: list[list[str]] = []

    def download(_version: str, path: Path) -> str:
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        return "https://example.test/install.sh"

    def runner(argv, **_kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    assert (
        run_update(
            dry_run=True,
            _fetch=lambda: [_release("99.0.0")],
            _download=download,
            _runner=runner,
            _manifest_path=manifest,
            _config_path=tmp_path / "config.yaml",
        )
        == 0
    )
    argv = seen[0]
    assert argv[argv.index("--method") + 1] == method
    assert "--dry-run" in argv


@pytest.mark.parametrize(
    ("relative_path", "kind"),
    [
        (".local/share/uv/tools/jarn/bin/jarn", "uv"),
        (".local/share/pipx/venvs/jarn/bin/jarn", "pipx"),
        ("lib/node_modules/jarn-cli/bin/jarn", "npm"),
        ("homebrew/Cellar/jarn/1.0/bin/jarn", "homebrew"),
        ("custom/bin/jarn", "unmanaged"),
    ],
)
def test_missing_receipt_path_inventory_is_explicit(
    tmp_path: Path,
    relative_path: str,
    kind: str,
) -> None:
    from jarn.update import _ownership_from_path

    path = tmp_path / relative_path
    ownership = _ownership_from_path(path, "2.0.0")

    assert ownership.kind == kind
    assert ownership.source == "executable-path inference; install record absent"
    assert ownership.updater_managed is False


def test_missing_receipt_detects_pip_console_script_without_claiming_curl_ownership(
    tmp_path: Path,
) -> None:
    from jarn.update import _ownership_from_path

    active = tmp_path / ".local" / "bin" / "jarn"
    active.parent.mkdir(parents=True)
    active.write_text(
        "#!/usr/bin/python3\nfrom jarn.cli import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )

    ownership = _ownership_from_path(active, "2.0.0")

    assert ownership.kind == "pip"
    assert ownership.updater_managed is False
    assert ownership.manager_command == (
        "python3",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "jarn==2.0.0",
    )


def test_invalid_action_record_refuses_before_download_or_execution(
    tmp_path: Path,
    capsys,
) -> None:
    from jarn.update import run_update

    active = tmp_path / "bin" / "jarn"
    outside = tmp_path / "other" / "jarn"
    manifest = tmp_path / "state" / "install.json"
    _command(active, "0.11.0")
    _command(outside, "0.11.0")
    _write_record(manifest, outside, version="0.11.0")
    mutations: list[str] = []

    assert (
        run_update(
            as_json=True,
            _fetch=lambda: [_release("99.0.0")],
            _download=lambda *_args: mutations.append("download") or "unused",
            _runner=lambda *_args, **_kwargs: (
                mutations.append("run") or subprocess.CompletedProcess([], 0)
            ),
            _manifest_path=manifest,
            _active_path=active,
            _config_path=tmp_path / "config.yaml",
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["preview"]["ownership"]["kind"] == "invalid-record"
    assert "outside the proven installer-owned" in payload["error"]["message"]
    assert mutations == []


def test_release_and_config_preview_is_bounded_redacted_and_precedes_download(
    tmp_path: Path,
    capsys,
) -> None:
    from jarn.update import run_update

    active = tmp_path / "bin" / "jarn"
    manifest = tmp_path / "state" / "install.json"
    config = tmp_path / "config.yaml"
    _command(active, "0.10.0")
    _write_record(manifest, active, version="0.10.0", method="binary")
    config.write_text(_V0_CONFIG, encoding="utf-8")
    original_config = config.read_bytes()
    secret = "sk-supersecretvalue1234567890"
    notes = (
        "JARN-CONFIG-SCHEMA: 3\n"
        "## Breaking changes\n"
        "- Remove the legacy profile key\n"
        "## Details\n"
        f"OPENAI_API_KEY={secret}\n" + ("x" * 5_000)
    )

    def download(_version: str, path: Path) -> str:
        output_before_download = capsys.readouterr().out
        assert "Version: 0.10.0 -> 99.0.0" in output_before_download
        assert "Remove the legacy profile key" in output_before_download
        assert secret not in output_before_download
        assert "schema 0 -> 1" in output_before_download
        assert "config.yaml.bak.<UTC timestamp>" in output_before_download
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        return "https://example.test/install.sh"

    assert (
        run_update(
            dry_run=True,
            _fetch=lambda: [
                _release(
                    "99.0.0",
                    name="Major release",
                    body=notes,
                    published_at="2026-08-09T00:00:00Z",
                )
            ],
            _download=download,
            _runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
            _manifest_path=manifest,
            _config_path=config,
        )
        == 0
    )
    assert config.read_bytes() == original_config
    assert not list(tmp_path.glob("config.yaml.bak.*"))


def test_json_preview_contains_bounded_primary_release_and_migration_plan(
    tmp_path: Path,
    capsys,
) -> None:
    from jarn.update import run_update

    active = tmp_path / "bin" / "jarn"
    manifest = tmp_path / "state" / "install.json"
    config = tmp_path / "config.yaml"
    _command(active, "0.10.0")
    _write_record(manifest, active, version="0.10.0", method="python")
    config.write_text(_V0_CONFIG, encoding="utf-8")
    secret = "sk-supersecretvalue1234567890"
    notes = (
        "Config schema: 4\n## Breaking Changes\n* Explicit provider rename\n## Details\n"
        f"TOKEN={secret}\n" + "z" * 5_000
    )

    def download(_version: str, path: Path) -> str:
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        return "https://example.test/install.sh"

    assert (
        run_update(
            as_json=True,
            dry_run=True,
            _fetch=lambda: [_release("99.0.0", name="Release 99", body=notes)],
            _download=download,
            _runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
            _manifest_path=manifest,
            _config_path=config,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    preview = payload["preview"]
    assert preview["currentVersion"] == "0.10.0"
    assert preview["targetVersion"] == "99.0.0"
    assert preview["ownership"]["installerMethod"] == "python"
    assert preview["release"]["url"].endswith("/releases/tag/v99.0.0")
    assert preview["release"]["notesTruncated"] is True
    assert len(preview["release"]["notes"]) <= 4_000
    assert secret not in json.dumps(payload)
    assert preview["release"]["breakingChanges"] == ["Explicit provider rename"]
    assert preview["config"]["sourceVersion"] == 0
    assert preview["config"]["targetVersion"] == 4
    assert preview["config"]["migrationSteps"] == [
        "schema 0 -> 1",
        "schema 1 -> 2",
        "schema 2 -> 3",
        "schema 3 -> 4",
    ]
    assert preview["config"]["backupRequired"] is True
    assert payload["changed"] is False


def test_explicit_version_must_exist_in_primary_release_catalog(
    tmp_path: Path,
    capsys,
) -> None:
    from jarn.update import run_update

    assert (
        run_update(
            as_json=True,
            version="98.0.0",
            _fetch=lambda: [_release("99.0.0")],
            _manifest_path=tmp_path / "state" / "install.json",
            _config_path=tmp_path / "config.yaml",
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "primary stable release catalog" in payload["error"]["message"]


def test_rollback_swaps_verified_commands_and_manifest(tmp_path: Path, capsys) -> None:
    from jarn.install_state import load_install_record
    from jarn.update import run_rollback

    active = tmp_path / "bin" / "jarn"
    previous = active.parent / ".jarn.rollback.1.0.0"
    _command(active, "2.0.0")
    _command(previous, "1.0.0")
    manifest = tmp_path / "state" / "install.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps(_record(active, previous)), encoding="utf-8")

    assert run_rollback(manifest_path=manifest) == 0
    assert subprocess.check_output([str(active), "--version"], text=True).strip() == "jarn 1.0.0"
    assert subprocess.check_output([str(previous), "--version"], text=True).strip() == "jarn 2.0.0"
    updated = load_install_record(manifest)
    assert updated.version == "1.0.0"
    assert updated.previous_path == previous
    assert "retained for a forward rollback" in capsys.readouterr().out


def test_rollback_rejects_broken_candidate_without_touching_active(tmp_path: Path) -> None:
    from jarn.update import run_rollback

    active = tmp_path / "bin" / "jarn"
    previous = active.parent / ".jarn.rollback.broken"
    _command(active, "2.0.0")
    previous.write_text("not executable", encoding="utf-8")
    manifest = tmp_path / "state" / "install.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps(_record(active, previous)), encoding="utf-8")

    assert run_rollback(manifest_path=manifest) == 1
    assert subprocess.check_output([str(active), "--version"], text=True).strip() == "jarn 2.0.0"


def test_rollback_post_activation_failure_restores_original(tmp_path: Path) -> None:
    from jarn.update import run_rollback

    active = tmp_path / "bin" / "jarn"
    previous = active.parent / ".jarn.rollback.1.0.0"
    _command(active, "2.0.0")
    _command(previous, "1.0.0")
    manifest = tmp_path / "state" / "install.json"
    manifest.parent.mkdir()
    original_record = _record(active, previous)
    manifest.write_text(json.dumps(original_record), encoding="utf-8")
    calls = 0

    def smoke(_path: Path) -> tuple[bool, str]:
        nonlocal calls
        calls += 1
        return (True, "") if calls == 1 else (False, "simulated post-activation fault")

    assert run_rollback(manifest_path=manifest, _smoke_check=smoke) == 1
    assert subprocess.check_output([str(active), "--version"], text=True).strip() == "jarn 2.0.0"
    assert subprocess.check_output([str(previous), "--version"], text=True).strip() == "jarn 1.0.0"
    assert json.loads(manifest.read_text(encoding="utf-8")) == original_record
