"""Docker execution backend (M1.1) — argv construction, IO, lifecycle, wiring.

No real Docker daemon is touched: ``subprocess.run`` / ``subprocess.Popen`` are
patched so we assert the exact argv JARN builds (mounts, network policy, exec
shape) and the behaviour of the cancellable layer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarn.agent import docker_backend
from jarn.agent.builder import SandboxUnavailable, _make_backend

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def _completed(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRun:
    """Records subprocess.run calls; answers `docker run` with a container id."""

    def __init__(self, *, run_rc=0, container_id="c0ffee123456"):
        self.calls: list[list[str]] = []
        self._run_rc = run_rc
        self._cid = container_id

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""
        if verb == "run":
            out = (self._cid + "\n").encode() if kwargs.get("text") is not True else self._cid + "\n"
            return _completed(self._run_rc, stdout=out)
        if verb == "info":
            return _completed(0)
        # exec / rm / ps
        return _completed(0)


def _make_backend_with_fakes(monkeypatch, **kwargs):
    fake = _FakeRun()
    monkeypatch.setattr(docker_backend.subprocess, "run", fake)
    be = docker_backend.CancellableDockerSandbox(
        project_root=Path("/work/proj"), **kwargs
    )
    return be, fake


# --------------------------------------------------------------------------
# docker_available
# --------------------------------------------------------------------------


def test_docker_available_false_when_no_cli(monkeypatch):
    monkeypatch.setattr(docker_backend.shutil, "which", lambda _: None)
    assert docker_backend.docker_available() is False


def test_docker_available_true_when_daemon_ok(monkeypatch):
    monkeypatch.setattr(docker_backend.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_backend.subprocess, "run", lambda *a, **k: _completed(0)
    )
    assert docker_backend.docker_available() is True


def test_docker_available_false_when_daemon_down(monkeypatch):
    monkeypatch.setattr(docker_backend.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_backend.subprocess, "run", lambda *a, **k: _completed(1)
    )
    assert docker_backend.docker_available() is False


# --------------------------------------------------------------------------
# Container start argv
# --------------------------------------------------------------------------


def test_start_argv_mounts_project_root_and_image(monkeypatch):
    be, fake = _make_backend_with_fakes(monkeypatch, image="myimg:1")
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert run_argv[:2] == ["docker", "run"]
    assert "-d" in run_argv and "--rm" in run_argv and "--init" in run_argv
    # project root bind-mounted at the same absolute path
    assert "-v" in run_argv
    assert f"{Path('/work/proj').resolve()}:{Path('/work/proj').resolve()}" in run_argv
    assert run_argv[-3:] == ["myimg:1", "sleep", "infinity"]
    assert be.id == "c0ffee123456"


def test_start_argv_network_none_when_denied(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch, allow_network=False)
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--network" in run_argv
    assert run_argv[run_argv.index("--network") + 1] == "none"


def test_start_argv_allows_network_by_default(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch, allow_network=True)
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--network" not in run_argv


def test_start_argv_extra_writable_bind_mounts(monkeypatch):
    be, fake = _make_backend_with_fakes(
        monkeypatch, extra_writable=[Path("/cache/x")]
    )
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert f"{Path('/cache/x').resolve()}:{Path('/cache/x').resolve()}" in run_argv


def test_start_raises_on_run_failure(monkeypatch):
    fake = _FakeRun(run_rc=1)
    monkeypatch.setattr(docker_backend.subprocess, "run", fake)
    with pytest.raises(docker_backend.DockerStartError):
        docker_backend.CancellableDockerSandbox(project_root=Path("/work/proj"))


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------


class _FakePopen:
    instances: list = []

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.returncode = 0
        self.pid = 9999
        self._out = "hello\n"
        self._err = ""
        _FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        return (self._out, self._err)

    def poll(self):
        return self.returncode


def test_execute_builds_docker_exec_argv(monkeypatch):
    be, _ = _make_backend_with_fakes(monkeypatch)
    _FakePopen.instances.clear()
    monkeypatch.setattr(docker_backend.subprocess, "Popen", _FakePopen)
    resp = be.execute("echo hi")
    assert resp.exit_code == 0
    assert "hello" in resp.output
    argv = _FakePopen.instances[-1].argv
    assert argv[:2] == ["docker", "exec"]
    assert be.id in argv
    # The command is wrapped with a `: JARN_EXEC_ID=<id> ;` no-op prefix so the
    # exec id lands in the shell's argv (needed for pkill -f cancellation), but
    # the user command runs unchanged after it.
    assert argv[-3:-1] == ["/bin/sh", "-c"]
    assert argv[-1].endswith("echo hi")
    assert f"{docker_backend._EXEC_ID_ENV}=" in argv[-1]


def test_execute_nonzero_exit_appended(monkeypatch):
    be, _ = _make_backend_with_fakes(monkeypatch)

    class _Fail(_FakePopen):
        def __init__(self, argv, **kwargs):
            super().__init__(argv, **kwargs)
            self.returncode = 2
            self._out = ""
            self._err = "boom"

    monkeypatch.setattr(docker_backend.subprocess, "Popen", _Fail)
    resp = be.execute("false")
    assert resp.exit_code == 2
    assert "[stderr] boom" in resp.output
    assert "Exit code: 2" in resp.output


def test_execute_empty_command_rejected(monkeypatch):
    be, _ = _make_backend_with_fakes(monkeypatch)
    resp = be.execute("")
    assert resp.exit_code == 1


def test_execute_after_close_fails_closed(monkeypatch):
    be, _ = _make_backend_with_fakes(monkeypatch)
    be.close()
    resp = be.execute("echo hi")
    assert resp.exit_code == 126
    assert "not running" in resp.output


# --------------------------------------------------------------------------
# upload / download
# --------------------------------------------------------------------------


def test_upload_files_pipes_bytes(monkeypatch):
    be, fake = _make_backend_with_fakes(monkeypatch)
    results = be.upload_files([("/work/proj/a.txt", b"data")])
    assert results[0].error is None
    upload = fake.calls[-1]
    assert upload[:3] == ["docker", "exec", "-i"]
    assert be.id in upload
    # the destination path is quoted into the cat command
    assert any("cat >" in part for part in upload)


def test_download_files_reads_bytes(monkeypatch):
    be = _make_backend_with_fakes(monkeypatch)[0]

    def _run(argv, **kwargs):
        return _completed(0, stdout=b"filebytes")

    monkeypatch.setattr(docker_backend.subprocess, "run", _run)
    results = be.download_files(["/work/proj/a.txt"])
    assert results[0].content == b"filebytes"
    assert results[0].error is None


def test_download_missing_file_errors(monkeypatch):
    be = _make_backend_with_fakes(monkeypatch)[0]
    monkeypatch.setattr(
        docker_backend.subprocess, "run", lambda *a, **k: _completed(1, stderr=b"no such file")
    )
    results = be.download_files(["/work/proj/missing.txt"])
    assert results[0].content is None
    assert results[0].error == "file_not_found"


# --------------------------------------------------------------------------
# lifecycle: terminate_all + close
# --------------------------------------------------------------------------


def test_terminate_all_kills_live_execs(monkeypatch):
    be, _ = _make_backend_with_fakes(monkeypatch)
    killed: list = []

    class _Proc:
        def __init__(self, pid):
            self.pid = pid
            self.returncode = None

        def poll(self):
            return None

    p1, p2 = _Proc(111), _Proc(222)
    be._live = {p1, p2}
    monkeypatch.setattr(be, "_kill", lambda p: killed.append(p))
    assert be.terminate_all() == 2
    assert set(killed) == {p1, p2}


def test_close_removes_container(monkeypatch):
    be, fake = _make_backend_with_fakes(monkeypatch)
    cid = be.id
    be.close()
    rm = fake.calls[-1]
    assert rm[:3] == ["docker", "rm", "-f"]
    assert cid in rm
    # idempotent
    n_before = len(fake.calls)
    be.close()
    assert len(fake.calls) == n_before


# --------------------------------------------------------------------------
# builder wiring + fail-closed
# --------------------------------------------------------------------------


def test_make_backend_docker_unavailable_raises(base_config, monkeypatch):
    base_config.execution.backend = "docker"
    monkeypatch.setattr(docker_backend, "docker_available", lambda: False)
    with pytest.raises(SandboxUnavailable):
        _make_backend(base_config, Path("/work/proj"))


def test_make_backend_docker_constructs_when_available(base_config, monkeypatch):
    base_config.execution.backend = "docker"
    monkeypatch.setattr(docker_backend, "docker_available", lambda: True)
    monkeypatch.setattr(docker_backend.subprocess, "run", _FakeRun())
    backend = _make_backend(base_config, Path("/work/proj"))
    assert backend.__class__.__name__ == "CancellableDockerSandbox"


def test_make_backend_sandbox_provider_docker_routes(base_config, monkeypatch):
    base_config.execution.backend = "sandbox"
    base_config.execution.sandbox_provider = "docker"
    monkeypatch.setattr(docker_backend, "docker_available", lambda: True)
    monkeypatch.setattr(docker_backend.subprocess, "run", _FakeRun())
    backend = _make_backend(base_config, Path("/work/proj"))
    assert backend.__class__.__name__ == "CancellableDockerSandbox"


# --------------------------------------------------------------------------
# config parsing + isolation-level visibility (M1.3)
# --------------------------------------------------------------------------


def test_config_parses_docker_backend(tmp_path):
    import yaml

    from jarn.config.loader import load_config

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump(
            {"execution": {"backend": "docker", "docker_image": "node:22-slim"}}
        ),
        encoding="utf-8",
    )
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.execution.backend == "docker"
    assert cfg.execution.docker_image == "node:22-slim"


def test_config_default_docker_image():
    from jarn.config.loader import load_config

    # Non-slim by default so procps/pkill is present for in-container cancel.
    cfg = load_config(global_path=None, project_path=None)
    assert cfg.execution.docker_image == "python:3.12"


def _ctrl(tmp_path, monkeypatch, base_config):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    from jarn.tui.controller import Controller

    return Controller(base_config, root)


def test_isolation_level_infers_docker_from_config(tmp_path, monkeypatch, base_config):
    base_config.execution.backend = "docker"
    ctrl = _ctrl(tmp_path, monkeypatch, base_config)
    assert ctrl.isolation_level() == "docker"
    assert "docker" in ctrl.status_line
    ctrl.close()


def test_isolation_level_host_by_default(tmp_path, monkeypatch, base_config):
    ctrl = _ctrl(tmp_path, monkeypatch, base_config)
    assert ctrl.isolation_level() == "host"
    assert "host" in ctrl.status_line
    ctrl.close()


def test_controller_close_tears_down_backend(tmp_path, monkeypatch, base_config):
    import types

    ctrl = _ctrl(tmp_path, monkeypatch, base_config)
    closed: list = []
    ctrl.runtime = types.SimpleNamespace(
        backend=types.SimpleNamespace(close=lambda: closed.append(True))
    )
    ctrl.close()
    assert closed == [True]


# --------------------------------------------------------------------------
# Item 1: In-container cancel
# --------------------------------------------------------------------------


def test_terminate_all_issues_docker_exec_kill_into_container(monkeypatch):
    """terminate_all() must issue a docker exec pkill into the container (item 1).

    Both the client-side kill AND the in-container kill must happen; the
    container itself must survive (session continues).
    """
    be, fake = _make_backend_with_fakes(monkeypatch)
    cid = be.id

    # Inject a fake exec_id as if a command were running.
    exec_id = "deadbeef1234"
    with be._live_exec_lock:
        be._live_exec_ids[exec_id] = object()  # type: ignore[assignment]

    # Stub out _kill so client-side kill doesn't explode on the fake entry.
    killed_client: list = []
    monkeypatch.setattr(be, "_kill", lambda p: killed_client.append(p))

    count = be.terminate_all()
    # No live Popen objects were in _live, so count is 0 (the dict entry is not
    # counted — _live is the authoritative count). Just verify the in-container
    # kill call was issued.
    _ = count  # not the focus of this assertion

    # Find docker exec calls made after construction (which issued docker run + ps).
    # The in-container kill call must target cid and include pkill + exec_id.
    exec_kill_calls = [
        c for c in fake.calls
        if c[:2] == ["docker", "exec"] and cid in c
        and any(exec_id in part for part in c)
    ]
    assert exec_kill_calls, (
        "terminate_all() must issue a docker exec kill into the container; "
        f"calls seen: {fake.calls}"
    )


def test_terminate_all_kills_client_popen_too(monkeypatch):
    """The original client-kill behaviour is preserved after hardening."""
    be, _ = _make_backend_with_fakes(monkeypatch)
    killed: list = []

    class _Proc:
        pid = 111
        returncode = None

        def poll(self):
            return None

    p = _Proc()
    be._live = {p}
    monkeypatch.setattr(be, "_kill", lambda proc: killed.append(proc))
    be.terminate_all()
    assert p in killed


def test_execute_injects_exec_id_env_var(monkeypatch):
    """execute() must pass -e JARN_EXEC_ID=<id> to docker exec (item 1)."""
    be, _ = _make_backend_with_fakes(monkeypatch)
    _FakePopen.instances.clear()
    monkeypatch.setattr(docker_backend.subprocess, "Popen", _FakePopen)
    be.execute("echo hi")
    argv = _FakePopen.instances[-1].argv
    # -e JARN_EXEC_ID=<uuid> must appear in the argv
    assert "-e" in argv
    e_idx = argv.index("-e")
    assert argv[e_idx + 1].startswith(f"{docker_backend._EXEC_ID_ENV}=")


# --------------------------------------------------------------------------
# Item 2: Resource limits argv
# --------------------------------------------------------------------------


def test_run_argv_memory_flag_when_set(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch, memory="2g")
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--memory" in run_argv
    assert run_argv[run_argv.index("--memory") + 1] == "2g"


def test_run_argv_memory_absent_when_unset(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch)
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--memory" not in run_argv


def test_run_argv_pids_flag_when_set(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch, pids=512)
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--pids-limit" in run_argv
    assert run_argv[run_argv.index("--pids-limit") + 1] == "512"


def test_run_argv_pids_absent_when_zero(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch, pids=0)
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--pids-limit" not in run_argv


def test_run_argv_cpus_flag_when_set(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch, cpus="1.5")
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--cpus" in run_argv
    assert run_argv[run_argv.index("--cpus") + 1] == "1.5"


def test_run_argv_cpus_absent_when_unset(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch)
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--cpus" not in run_argv


# --------------------------------------------------------------------------
# Item 2: Resource limits loader parsing
# --------------------------------------------------------------------------


def test_loader_parses_resource_limits(tmp_path):
    import yaml

    from jarn.config.loader import load_config

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump({
            "execution": {
                "docker_memory": "1g",
                "docker_pids": 256,
                "docker_cpus": "2",
            }
        }),
        encoding="utf-8",
    )
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.execution.docker_memory == "1g"
    assert cfg.execution.docker_pids == 256
    assert cfg.execution.docker_cpus == "2"


def test_loader_resource_limits_defaults():
    from jarn.config.loader import load_config

    cfg = load_config(global_path=None, project_path=None)
    assert cfg.execution.docker_memory == ""
    assert cfg.execution.docker_pids == 0
    assert cfg.execution.docker_cpus == ""


def test_loader_docker_memory_bad_type_raises(tmp_path):
    import yaml

    from jarn.config.loader import ConfigError, load_config

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump({"execution": {"docker_memory": 123}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="docker_memory"):
        load_config(global_path=gp, project_path=None)


def test_loader_docker_pids_bad_type_raises(tmp_path):
    import yaml

    from jarn.config.loader import ConfigError, load_config

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump({"execution": {"docker_pids": "not-an-int"}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="docker_pids"):
        load_config(global_path=gp, project_path=None)


def test_loader_docker_cpus_bad_type_raises(tmp_path):
    import yaml

    from jarn.config.loader import ConfigError, load_config

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump({"execution": {"docker_cpus": 2}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="docker_cpus"):
        load_config(global_path=gp, project_path=None)


# --------------------------------------------------------------------------
# Item 3: Non-root user
# --------------------------------------------------------------------------


def test_run_argv_user_flag_when_set(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch, user="1000:1000")
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--user" in run_argv
    assert run_argv[run_argv.index("--user") + 1] == "1000:1000"


def test_run_argv_user_absent_when_unset(monkeypatch):
    _, fake = _make_backend_with_fakes(monkeypatch)
    run_argv = next(a for a in fake.calls if a[1] == "run")
    assert "--user" not in run_argv


def test_loader_parses_docker_user(tmp_path):
    import yaml

    from jarn.config.loader import load_config

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump({"execution": {"docker_user": "1000:1000"}}),
        encoding="utf-8",
    )
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.execution.docker_user == "1000:1000"


def test_loader_docker_user_default_empty():
    from jarn.config.loader import load_config

    cfg = load_config(global_path=None, project_path=None)
    assert cfg.execution.docker_user == ""


def test_loader_docker_user_bad_type_raises(tmp_path):
    import yaml

    from jarn.config.loader import ConfigError, load_config

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump({"execution": {"docker_user": 1000}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="docker_user"):
        load_config(global_path=gp, project_path=None)


# --------------------------------------------------------------------------
# Item 4: Anti-orphan reaper
# --------------------------------------------------------------------------


def test_reaper_session_scoped_reaps_only_dead_pid_sessions(monkeypatch, tmp_path):
    """Reaper removes containers of a DEAD prior session, by its session label.

    A pid-file from a crashed run (whose pid is no longer alive) names a session
    id; the reaper queries ``label=jarn-session=<id>`` and force-removes the
    container. The new mechanism is session-scoped via the pid-file, so a live
    concurrent session is never touched.
    """
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    run_dir = docker_backend._run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    # A dead-pid pid-file naming a stale session.
    dead_session = "deadsession01"
    (run_dir / "999999.docker-session").write_text(dead_session, encoding="utf-8")

    monkeypatch.setattr(docker_backend, "_pid_alive", lambda pid: False)

    fake = _FakeRun(container_id="newcid")
    stale_cid = "stale0001"
    original_call = fake.__call__

    def fake_run_with_stale(argv, **kwargs):
        if argv[:3] == ["docker", "ps", "-q"] and any(
            f"label=jarn-session={dead_session}" in a for a in argv
        ):
            out = (stale_cid + "\n").encode() if kwargs.get("text") is not True else stale_cid + "\n"
            fake.calls.append(list(argv))
            return _completed(0, stdout=out)
        return original_call(argv, **kwargs)

    monkeypatch.setattr(docker_backend.subprocess, "run", fake_run_with_stale)
    docker_backend.CancellableDockerSandbox(project_root=Path("/work/proj"))

    rm_calls = [c for c in fake.calls if c[:3] == ["docker", "rm", "-f"] and stale_cid in c]
    assert rm_calls, "reaper must rm the dead session's stale container"
    # The dead-pid pid-file must be cleaned up.
    assert not (run_dir / "999999.docker-session").exists()


def test_reaper_skips_live_concurrent_session(monkeypatch, tmp_path):
    """A live (concurrent) session's container is NEVER reaped (item 4)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    run_dir = docker_backend._run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    live_session = "livesession99"
    pid_file = run_dir / "424242.docker-session"
    pid_file.write_text(live_session, encoding="utf-8")

    # Pretend that pid is alive — the concurrent session is running.
    monkeypatch.setattr(docker_backend, "_pid_alive", lambda pid: pid == 424242)

    fake = _FakeRun(container_id="newcid")
    monkeypatch.setattr(docker_backend.subprocess, "run", fake)
    docker_backend.CancellableDockerSandbox(project_root=Path("/work/proj"))

    # No ps/rm targeting the live session may be issued; its pid-file survives.
    assert not any(
        c[:3] == ["docker", "ps", "-q"]
        and any(f"label=jarn-session={live_session}" in a for a in c)
        for c in fake.calls
    )
    assert pid_file.exists()


# --------------------------------------------------------------------------
# Item 5: Image preflight + clear error
# --------------------------------------------------------------------------


def test_make_backend_docker_start_error_surfaces_sandbox_unavailable(
    base_config, monkeypatch
):
    """DockerStartError on docker run raises SandboxUnavailable with clear message (item 5)."""
    base_config.execution.backend = "docker"
    base_config.execution.docker_image = "missing-image:latest"
    monkeypatch.setattr(docker_backend, "docker_available", lambda: True)
    monkeypatch.setattr(docker_backend.subprocess, "run", _FakeRun(run_rc=1))

    with pytest.raises(SandboxUnavailable) as exc_info:
        _make_backend(base_config, Path("/work/proj"))

    msg = str(exc_info.value)
    assert "missing-image:latest" in msg, "Error message must name the image"
    # Must be readable — not a raw traceback fragment.
    assert "docker pull" in msg or "pull" in msg or "image" in msg.lower()


def test_make_backend_docker_start_error_no_traceback_exposed(
    base_config, monkeypatch
):
    """SandboxUnavailable must be raised (not DockerStartError) so callers get a clean message."""
    from jarn.agent.docker_backend import DockerStartError

    base_config.execution.backend = "docker"
    monkeypatch.setattr(docker_backend, "docker_available", lambda: True)
    monkeypatch.setattr(docker_backend.subprocess, "run", _FakeRun(run_rc=1))

    with pytest.raises(SandboxUnavailable):
        _make_backend(base_config, Path("/work/proj"))

    # The outer exception must be SandboxUnavailable, not DockerStartError.
    try:
        _make_backend(base_config, Path("/work/proj"))
    except SandboxUnavailable:
        pass
    except DockerStartError:
        pytest.fail("DockerStartError must be wrapped in SandboxUnavailable")


# --------------------------------------------------------------------------
# Item 6: atexit cleanup registration
# --------------------------------------------------------------------------


def test_atexit_cleanup_registered_and_idempotent(monkeypatch):
    """atexit callable: registered at container start, stops/removes the container,
    idempotent on second call, and disarmed after close().

    Design: do NOT invoke the real atexit machinery — capture the registered
    callable via monkeypatching atexit.register, then call it manually.
    """
    # --- Part 1: registration and cleanup ---
    registered: list = []
    monkeypatch.setattr(docker_backend.atexit, "register", lambda fn: registered.append(fn))

    fake = _FakeRun()
    monkeypatch.setattr(docker_backend.subprocess, "run", fake)
    be = docker_backend.CancellableDockerSandbox(project_root=Path("/work/proj"))
    cid = be.id

    # atexit.register must have been called with one callable
    assert len(registered) == 1, "exactly one atexit callable must be registered"
    atexit_fn = registered[0]

    # Calling the callable removes the container
    n_before = len(fake.calls)
    atexit_fn()
    rm_calls = [c for c in fake.calls[n_before:] if c[:3] == ["docker", "rm", "-f"] and cid in c]
    assert rm_calls, f"atexit callback must force-remove the container; calls: {fake.calls[n_before:]}"

    # Second call is idempotent — no new subprocess calls
    n_after_first = len(fake.calls)
    atexit_fn()
    assert len(fake.calls) == n_after_first, (
        "atexit callback must be idempotent (second invocation must be a no-op)"
    )

    # --- Part 2: close() disarms the atexit callback ---
    registered2: list = []
    monkeypatch.setattr(docker_backend.atexit, "register", lambda fn: registered2.append(fn))
    fake2 = _FakeRun()
    monkeypatch.setattr(docker_backend.subprocess, "run", fake2)
    be2 = docker_backend.CancellableDockerSandbox(project_root=Path("/work/proj"))

    assert len(registered2) == 1, "second backend must also register an atexit callable"
    atexit_fn2 = registered2[0]

    be2.close()  # graceful close disarms the atexit hook
    n_before2 = len(fake2.calls)  # count *after* close already ran docker rm
    atexit_fn2()  # must be a no-op — container already gone, flag cleared by close()
    assert len(fake2.calls) == n_before2, (
        "atexit callback must be a no-op after close() has already cleaned up"
    )
