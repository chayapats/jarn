"""Tests for the OS-level execution sandbox (jarn.agent.os_sandbox).

This is security-critical code: tests are thorough and deterministic.  The
macOS and Linux wrap() branches are each callable directly by patching
sys.platform / shutil.which, so tests run on any host without requiring the
real sandbox tools to be present.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import jarn.agent.os_sandbox as sbx
from jarn.agent.os_sandbox import (
    _linux_argv,
    _macos_argv,
    _macos_profile,
    available,
    backend_name,
    default_writable,
    wrap,
)

# ---------------------------------------------------------------------------
# backend_name() / available() — detection
# ---------------------------------------------------------------------------


class TestBackendName:
    def test_returns_sandbox_exec_on_macos_when_on_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None)
        assert backend_name() == "sandbox-exec"

    def test_returns_none_on_macos_when_not_on_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert backend_name() is None

    def test_returns_bwrap_on_linux_when_on_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
        assert backend_name() == "bwrap"

    def test_returns_none_on_linux_when_not_on_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert backend_name() is None

    def test_returns_none_on_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        # Even if bwrap were somehow on PATH it should return None on win32.
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap")
        assert backend_name() is None

    def test_available_true_when_backend_name_is_set(self, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: "bwrap")
        assert available() is True

    def test_available_false_when_backend_name_is_none(self, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: None)
        assert available() is False


# ---------------------------------------------------------------------------
# macOS branch — profile and argv
# ---------------------------------------------------------------------------


class TestMacOSProfile:
    """_macos_profile() generates valid SBPL strings."""

    def test_allows_project_root_as_writable(self, tmp_path):
        profile = _macos_profile(
            project_root=tmp_path,
            allow_network=True,
            writable=[],
        )
        assert f'(subpath "{tmp_path}")' in profile

    def test_always_allows_tmpdir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", "/custom/tmp")
        profile = _macos_profile(
            project_root=tmp_path,
            allow_network=True,
            writable=[],
        )
        assert '(subpath "/custom/tmp")' in profile

    def test_denies_file_write_globally(self, tmp_path):
        profile = _macos_profile(
            project_root=tmp_path,
            allow_network=True,
            writable=[],
        )
        assert "(deny file-write*" in profile
        # The global deny must cover /
        assert '(subpath "/")' in profile

    def test_allows_additional_writable_paths(self, tmp_path):
        extra = Path("/some/cache")
        profile = _macos_profile(
            project_root=tmp_path,
            allow_network=True,
            writable=[extra],
        )
        assert f'(subpath "{extra}")' in profile

    def test_network_denied_when_disabled(self, tmp_path):
        profile = _macos_profile(
            project_root=tmp_path,
            allow_network=False,
            writable=[],
        )
        assert "(deny network*)" in profile

    def test_network_not_denied_when_allowed(self, tmp_path):
        profile = _macos_profile(
            project_root=tmp_path,
            allow_network=True,
            writable=[],
        )
        assert "(deny network*)" not in profile

    def test_profile_starts_with_version_and_allow_default(self, tmp_path):
        profile = _macos_profile(
            project_root=tmp_path,
            allow_network=True,
            writable=[],
        )
        assert profile.startswith("(version 1)")
        assert "(allow default)" in profile


class TestMacOSArgv:
    """_macos_argv() / wrap() on macOS produce the right argv list."""

    def test_argv_starts_with_sandbox_exec(self, tmp_path):
        argv = _macos_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[])
        assert argv[0] == "sandbox-exec"

    def test_argv_passes_profile_via_p_flag(self, tmp_path):
        argv = _macos_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[])
        assert argv[1] == "-p"
        # The profile string is in argv[2].
        profile = argv[2]
        assert "(version 1)" in profile

    def test_argv_ends_with_sh_c_command(self, tmp_path):
        cmd = "ls -la"
        argv = _macos_argv(cmd, project_root=tmp_path, allow_network=True, writable=[])
        assert argv[-3] == "/bin/sh"
        assert argv[-2] == "-c"
        assert argv[-1] == cmd

    def test_wrap_on_macos_delegates_to_macos_argv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: "sandbox-exec")
        argv = wrap("pwd", project_root=tmp_path, allow_network=True, writable=[])
        assert argv[0] == "sandbox-exec"

    def test_wrap_includes_deny_network_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: "sandbox-exec")
        argv = wrap("pwd", project_root=tmp_path, allow_network=False, writable=[])
        # The profile is argv[2]
        assert "(deny network*)" in argv[2]

    def test_wrap_no_deny_network_when_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: "sandbox-exec")
        argv = wrap("pwd", project_root=tmp_path, allow_network=True, writable=[])
        assert "(deny network*)" not in argv[2]

    def test_wrap_profile_allows_project_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: "sandbox-exec")
        argv = wrap("pwd", project_root=tmp_path, allow_network=True, writable=[])
        assert str(tmp_path) in argv[2]


# ---------------------------------------------------------------------------
# Linux branch — bwrap argv
# ---------------------------------------------------------------------------


class TestLinuxArgv:
    """_linux_argv() / wrap() on Linux produce the right argv list."""

    def test_argv_starts_with_bwrap(self, tmp_path):
        argv = _linux_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[])
        assert argv[0] == "bwrap"

    def test_argv_contains_ro_bind_root(self, tmp_path):
        argv = _linux_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[])
        # --ro-bind / / must be present
        pairs = list(zip(argv, argv[1:], argv[2:], strict=False))
        assert any(a == "--ro-bind" and b == "/" and c == "/" for a, b, c in pairs)

    def test_argv_bind_mounts_project_root_writable(self, tmp_path):
        argv = _linux_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[])
        root_str = str(tmp_path)
        pairs = list(zip(argv, argv[1:], argv[2:], strict=False))
        assert any(a == "--bind" and b == root_str and c == root_str for a, b, c in pairs)

    def test_argv_contains_tmpfs_dev_proc(self, tmp_path):
        argv = _linux_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[])
        assert "--tmpfs" in argv
        assert "--dev" in argv
        assert "--proc" in argv

    def test_argv_die_with_parent(self, tmp_path):
        argv = _linux_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[])
        assert "--die-with-parent" in argv

    def test_unshare_net_when_network_disabled(self, tmp_path):
        argv = _linux_argv("echo hello", project_root=tmp_path, allow_network=False, writable=[])
        assert "--unshare-net" in argv

    def test_no_unshare_net_when_network_allowed(self, tmp_path):
        argv = _linux_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[])
        assert "--unshare-net" not in argv

    def test_extra_writable_paths_are_bound(self, tmp_path):
        extra = Path("/home/user/.cache")
        argv = _linux_argv("echo hello", project_root=tmp_path, allow_network=True, writable=[extra])
        # Paths are canonicalized before binding (symlink-safe), so the bind
        # target is the resolved form, not the literal string passed in.
        extra_str = str(extra.resolve())
        pairs = list(zip(argv, argv[1:], argv[2:], strict=False))
        assert any(a == "--bind" and b == extra_str and c == extra_str for a, b, c in pairs)

    def test_argv_ends_with_sh_c_command(self, tmp_path):
        cmd = "make test"
        argv = _linux_argv(cmd, project_root=tmp_path, allow_network=True, writable=[])
        assert argv[-3] == "/bin/sh"
        assert argv[-2] == "-c"
        assert argv[-1] == cmd

    def test_wrap_on_linux_delegates_to_linux_argv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: "bwrap")
        argv = wrap("pwd", project_root=tmp_path, allow_network=True, writable=[])
        assert argv[0] == "bwrap"

    def test_wrap_linux_unshare_net(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: "bwrap")
        argv = wrap("pwd", project_root=tmp_path, allow_network=False, writable=[])
        assert "--unshare-net" in argv


# ---------------------------------------------------------------------------
# wrap() — error path
# ---------------------------------------------------------------------------


class TestWrapNoBackend:
    def test_raises_runtimeerror_when_no_backend(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sbx, "backend_name", lambda: None)
        with pytest.raises(RuntimeError, match="No OS sandbox backend"):
            wrap("echo hello", project_root=tmp_path, allow_network=True, writable=[])


# ---------------------------------------------------------------------------
# default_writable()
# ---------------------------------------------------------------------------


class TestDefaultWritable:
    def test_returns_list_of_paths(self, tmp_path):
        result = default_writable(tmp_path)
        assert isinstance(result, list)
        assert all(isinstance(p, Path) for p in result)

    def test_only_existing_paths_returned(self, tmp_path, monkeypatch):
        # Point home to tmp_path so none of ~/.cache etc. exist.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp_nonexistent"))
        result = default_writable(tmp_path)
        for p in result:
            assert p.exists(), f"non-existent path returned: {p}"

    def test_system_tmp_included_when_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))  # tmp_path always exists
        result = default_writable(tmp_path)
        assert tmp_path in result


# ---------------------------------------------------------------------------
# CancellableLocalShellBackend integration — sandbox_mode
# ---------------------------------------------------------------------------


class TestBackendSandboxOff:
    """With sandbox_mode='off', Popen must use shell=True (original behavior)."""

    def test_shell_true_when_off(self, tmp_path):
        from jarn.agent.local_backend import CancellableLocalShellBackend

        popen_calls: list[dict] = []

        class _FakePopen:
            returncode = 0

            def __init__(self, *args, **kwargs):
                popen_calls.append({"args": args, "kwargs": kwargs})
                self.returncode = 0

            def communicate(self, timeout=None):
                return "hello\n", ""

            def poll(self):
                return 0

        backend = CancellableLocalShellBackend(
            root_dir=str(tmp_path),
            virtual_mode=True,
            sandbox_mode="off",
            project_root=tmp_path,
        )
        with patch("jarn.agent.local_backend.subprocess.Popen", _FakePopen):
            backend.execute("echo hello")

        assert popen_calls, "Popen was never called"
        call = popen_calls[0]
        # When sandbox is off, shell=True and the first arg is the raw command string.
        assert call["kwargs"].get("shell") is True
        assert call["args"][0] == "echo hello"


class TestBackendSandboxRequireUnavailable:
    """With sandbox_mode='require' and no backend, execute must fail closed."""

    def test_fails_closed_without_running_command(self, tmp_path, monkeypatch):
        from jarn.agent.local_backend import CancellableLocalShellBackend

        monkeypatch.setattr("jarn.agent.os_sandbox.available", lambda: False)
        monkeypatch.setattr("jarn.agent.os_sandbox.backend_name", lambda: None)

        backend = CancellableLocalShellBackend(
            root_dir=str(tmp_path),
            virtual_mode=True,
            sandbox_mode="require",
            project_root=tmp_path,
        )
        res = backend.execute("echo hello")
        assert res.exit_code != 0
        assert "require" in res.output.lower() or "sandbox" in res.output.lower()

    def test_does_not_call_popen_when_failing_closed(self, tmp_path, monkeypatch):
        from jarn.agent.local_backend import CancellableLocalShellBackend

        monkeypatch.setattr("jarn.agent.os_sandbox.available", lambda: False)
        monkeypatch.setattr("jarn.agent.os_sandbox.backend_name", lambda: None)

        popen_called = []
        with patch("jarn.agent.local_backend.subprocess.Popen", lambda *a, **k: popen_called.append(True)):
            backend = CancellableLocalShellBackend(
                root_dir=str(tmp_path),
                virtual_mode=True,
                sandbox_mode="require",
                project_root=tmp_path,
            )
            backend.execute("echo hello")

        assert not popen_called, "Popen should not be called when failing closed"


class TestBackendSandboxAutoAvailable:
    """With sandbox_mode='auto' and a backend available, Popen uses shell=False + argv."""

    def test_popen_called_with_shell_false_and_list_argv(self, tmp_path, monkeypatch):
        from jarn.agent.local_backend import CancellableLocalShellBackend

        # Patch in the canonical module so local_backend's import sees bwrap.
        monkeypatch.setattr("jarn.agent.os_sandbox.backend_name", lambda: "bwrap")
        monkeypatch.setattr("jarn.agent.os_sandbox.available", lambda: True)

        popen_calls: list[dict] = []

        class _FakePopen:
            returncode = 0

            def __init__(self, *args, **kwargs):
                popen_calls.append({"args": args, "kwargs": kwargs})

            def communicate(self, timeout=None):
                return "ok\n", ""

            def poll(self):
                return 0

        backend = CancellableLocalShellBackend(
            root_dir=str(tmp_path),
            virtual_mode=True,
            sandbox_mode="auto",
            project_root=tmp_path,
            sandbox_allow_network=True,
        )
        with patch("jarn.agent.local_backend.subprocess.Popen", _FakePopen):
            backend.execute("echo hello")

        assert popen_calls, "Popen was never called"
        call = popen_calls[0]
        # shell=False — the argv list is passed directly.
        assert call["kwargs"].get("shell") is False
        # The first arg must be a list (argv), not a string.
        assert isinstance(call["args"][0], list)
        assert call["args"][0][0] == "bwrap"


class TestBackendSandboxAutoUnavailable:
    """With sandbox_mode='auto' and no backend, fallback to shell=True with a warning."""

    def test_falls_back_to_shell_true_and_warns(self, tmp_path, monkeypatch):
        """Degraded-auto: Popen uses shell=True and a warning is emitted."""
        import logging

        from jarn.agent.local_backend import (
            _WARNED_SANDBOX_UNAVAILABLE,
            CancellableLocalShellBackend,
        )

        # Patch available() and backend_name() in the canonical module location so
        # the code path inside local_backend._build_sandbox_argv sees None.
        monkeypatch.setattr("jarn.agent.os_sandbox.available", lambda: False)
        monkeypatch.setattr("jarn.agent.os_sandbox.backend_name", lambda: None)

        popen_calls: list[dict] = []

        class _FakePopen:
            returncode = 0

            def __init__(self, *args, **kwargs):
                popen_calls.append({"args": args, "kwargs": kwargs})

            def communicate(self, timeout=None):
                return "hello\n", ""

            def poll(self):
                return 0

        # Clear the entire warning-dedup set before we create the backend so the
        # warning fires unconditionally (id reuse across tests is legal in CPython).
        _WARNED_SANDBOX_UNAVAILABLE.clear()

        backend = CancellableLocalShellBackend(
            root_dir=str(tmp_path),
            virtual_mode=True,
            sandbox_mode="auto",
            project_root=tmp_path,
        )

        # Capture via a real handler on the module logger so we don't depend on
        # caplog's propagation setup, which can vary between test-suite runs.
        import io
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.WARNING)
        mod_logger = logging.getLogger("jarn.agent.local_backend")
        mod_logger.addHandler(handler)
        try:
            with patch("jarn.agent.local_backend.subprocess.Popen", _FakePopen):
                backend.execute("echo hello")
        finally:
            mod_logger.removeHandler(handler)

        assert popen_calls, "Popen was never called"
        call = popen_calls[0]
        assert call["kwargs"].get("shell") is True
        assert "sandbox" in buf.getvalue().lower(), (
            f"Expected a sandbox-unavailable warning but got: {buf.getvalue()!r}"
        )


# ---------------------------------------------------------------------------
# Config parsing — new execution fields
# ---------------------------------------------------------------------------


class TestConfigParsingNewFields:
    def test_local_sandbox_off_is_default(self):
        from jarn.config.loader import load_config

        cfg = load_config(global_path=None, project_path=None)
        assert cfg.execution.local_sandbox == "off"

    def test_sandbox_allow_network_default_true(self):
        from jarn.config.loader import load_config

        cfg = load_config(global_path=None, project_path=None)
        assert cfg.execution.sandbox_allow_network is True

    def test_sandbox_writable_default_empty(self):
        from jarn.config.loader import load_config

        cfg = load_config(global_path=None, project_path=None)
        assert cfg.execution.sandbox_writable == []

    def test_parses_local_sandbox_auto(self, tmp_path):
        import yaml

        from jarn.config.loader import load_config

        gp = tmp_path / "g.yaml"
        gp.write_text(
            yaml.safe_dump({"execution": {"local_sandbox": "auto"}}),
            encoding="utf-8",
        )
        cfg = load_config(global_path=gp, project_path=None)
        assert cfg.execution.local_sandbox == "auto"

    def test_parses_local_sandbox_require(self, tmp_path):
        import yaml

        from jarn.config.loader import load_config

        gp = tmp_path / "g.yaml"
        gp.write_text(
            yaml.safe_dump({"execution": {"local_sandbox": "require"}}),
            encoding="utf-8",
        )
        cfg = load_config(global_path=gp, project_path=None)
        assert cfg.execution.local_sandbox == "require"

    def test_invalid_local_sandbox_value_raises_config_error(self, tmp_path):
        import yaml

        from jarn.config.loader import ConfigError, load_config

        gp = tmp_path / "g.yaml"
        gp.write_text(
            yaml.safe_dump({"execution": {"local_sandbox": "maybe"}}),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="local_sandbox"):
            load_config(global_path=gp, project_path=None)

    def test_parses_sandbox_allow_network_false(self, tmp_path):
        import yaml

        from jarn.config.loader import load_config

        gp = tmp_path / "g.yaml"
        gp.write_text(
            yaml.safe_dump({"execution": {"sandbox_allow_network": False}}),
            encoding="utf-8",
        )
        cfg = load_config(global_path=gp, project_path=None)
        assert cfg.execution.sandbox_allow_network is False

    def test_parses_sandbox_writable_list(self, tmp_path):
        import yaml

        from jarn.config.loader import load_config

        gp = tmp_path / "g.yaml"
        gp.write_text(
            yaml.safe_dump({"execution": {"sandbox_writable": ["/tmp/foo", "/var/cache"]}}),
            encoding="utf-8",
        )
        cfg = load_config(global_path=gp, project_path=None)
        assert cfg.execution.sandbox_writable == ["/tmp/foo", "/var/cache"]

    def test_sandbox_writable_non_list_raises_config_error(self, tmp_path):
        import yaml

        from jarn.config.loader import ConfigError, load_config

        gp = tmp_path / "g.yaml"
        gp.write_text(
            yaml.safe_dump({"execution": {"sandbox_writable": "/single/path"}}),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="sandbox_writable"):
            load_config(global_path=gp, project_path=None)


# ---------------------------------------------------------------------------
# Doctor diagnostics — sandbox field
# ---------------------------------------------------------------------------


class TestDoctorSandboxDiagnostic:
    def test_doctor_includes_sandbox_field(self, tmp_path, monkeypatch):
        """_collect_doctor populates diag['sandbox'] with backend, available, mode."""
        import yaml

        from jarn.cli import _collect_doctor

        # Minimal config that satisfies _collect_doctor.
        gp = tmp_path / "g.yaml"
        gp.write_text(
            yaml.safe_dump(
                {
                    "default_profile": "openrouter",
                    "providers": {
                        "openrouter": {
                            "type": "openrouter",
                            "api_key": "sk-test",
                            "base_url": "http://localhost:9999/v1",
                        }
                    },
                    "routing": {"main": "openrouter/some-model"},
                    "execution": {"local_sandbox": "auto"},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("JARN_HOME", str(tmp_path))
        monkeypatch.setattr("jarn.config.paths.global_config_path", lambda: gp)
        monkeypatch.setattr("jarn.config.paths.find_project_root", lambda: None)

        diag: dict = {}
        _collect_doctor(diag)

        assert "sandbox" in diag
        sbx_diag = diag["sandbox"]
        assert "available" in sbx_diag
        assert "backend" in sbx_diag
        assert sbx_diag["mode"] == "auto"


# ---------------------------------------------------------------------------
# Real end-to-end integration — gated on actual tool availability
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    backend_name() is None,
    reason="No OS sandbox tool available on this host — skipping real sandbox test",
)
def test_sandbox_denies_write_outside_project(tmp_path):
    """Run a real sandboxed command that tries to write outside the project.

    The write should be denied by the OS kernel (non-zero exit or no file).
    This test is skipped if sandbox-exec / bwrap is not on PATH.
    """
    sentinel = Path.home() / ".jarn_sbx_test"
    # Ensure the sentinel doesn't exist before the test.
    sentinel.unlink(missing_ok=True)

    try:
        cmd = f"echo x > {sentinel}"
        argv = wrap(
            cmd,
            project_root=tmp_path,
            allow_network=True,
            writable=[],  # home is NOT in the writable set
        )
        result = subprocess.run(  # noqa: S603
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Either the command failed (non-zero exit) or the file was not created.
        # Both are acceptable — the kernel may deny silently or with an error.
        denied = (result.returncode != 0) or (not sentinel.exists())
        assert denied, (
            f"Sandbox failed to deny write to {sentinel}. "
            f"Exit: {result.returncode}, stderr: {result.stderr!r}"
        )
    finally:
        sentinel.unlink(missing_ok=True)


class TestSymlinkCanonicalization:
    """Regression: profile/argv paths must be resolved (symlink-safe).

    Seatbelt matches a write against the target's *resolved* path; on macOS
    `/var` and `/tmp` are symlinks into `/private/...`. An unresolved subpath
    would silently deny writes inside the project itself.
    """

    def test_macos_profile_uses_resolved_project_path(self, tmp_path):
        from jarn.agent.os_sandbox import _macos_profile

        real = tmp_path / "real_proj"
        real.mkdir()
        link = tmp_path / "linked_proj"
        link.symlink_to(real)
        profile = _macos_profile(project_root=link, allow_network=True, writable=[])
        assert str(real.resolve()) in profile
        # The symlink path itself must NOT be the allow target.
        assert f'(subpath "{link}")' not in profile

    def test_linux_argv_uses_resolved_project_path(self, tmp_path):
        from jarn.agent.os_sandbox import _linux_argv

        real = tmp_path / "real_proj"
        real.mkdir()
        link = tmp_path / "linked_proj"
        link.symlink_to(real)
        argv = _linux_argv("echo hi", project_root=link, allow_network=True, writable=[])
        assert str(real.resolve()) in argv
        assert str(link) not in argv
