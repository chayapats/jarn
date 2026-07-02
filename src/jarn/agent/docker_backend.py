"""Docker container execution backend — real OS-level isolation for shell + fs.

Unlike the host :class:`~jarn.agent.local_backend.CancellableLocalShellBackend`
(where the permission engine is the *only* authorizer) or the OS sandbox
(``sandbox-exec`` / ``bwrap`` process wrapping), this backend runs every shell
command and every filesystem mutation **inside a Docker container** whose only
window onto the host is a bind-mount of the project root. Everything else the
agent can touch is the container image's own filesystem — escaping it requires a
container breakout, not just slipping past a regex. This is the isolation story
that lets someone run JARN on an untrusted repo without trusting the code it runs.

It subclasses deepagents' :class:`BaseSandbox`, which derives ``ls``/``grep``/
``glob``/``read``/``write``/``edit`` from three primitives: :meth:`execute` (a
shell), :meth:`upload_files` and :meth:`download_files` (byte transfer). We
implement those over ``docker exec`` (with bytes piped via stdin), so no extra
in-container agent is required.

Container lifecycle: a detached ``docker run ... sleep infinity`` keeps a
long-lived container for the session; each tool call ``docker exec``s into it.
The project root is bind-mounted at the **same absolute path** inside the
container so the paths the model reasons about match one-to-one. :meth:`close`
removes the container; :meth:`terminate_all` kills in-flight ``docker exec``
clients on turn cancel **without** tearing the container down (the session
continues).

This module shells out to the ``docker`` CLI rather than the SDK to keep the
dependency surface zero — ``docker`` on PATH + a reachable daemon is the only
requirement (see :func:`docker_available`).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from jarn.agent.process_util import terminate_process_group

logger = logging.getLogger("jarn.agent.docker_backend")

#: Default container image. Must ship ``python3`` and ``/bin/sh`` because
#: BaseSandbox derives glob/edit/read via inline ``python3 -c`` scripts. We use
#: the *non-slim* ``python:3.12`` rather than ``-slim`` on purpose: in-container
#: turn cancellation relies on ``pkill -f`` (see :meth:`_kill_in_container`),
#: which lives in the ``procps`` package that slim images omit. The full image
#: ships it, so cancel works out of the box. Users wanting node/ripgrep/git (or a
#: smaller image, accepting weaker in-container cancel) set their own via
#: ``execution.docker_image``.
DEFAULT_IMAGE = "python:3.12"

#: How long (seconds) to wait for one-off control commands (run/rm/info).
_CONTROL_TIMEOUT = 60

#: Environment variable name used to tag each ``docker exec`` with a unique
#: execution ID. It is set both as a ``docker exec -e`` env var (so child
#: processes inherit it) AND embedded as a literal ``: JARN_EXEC_ID=<id> ;``
#: no-op prefix in the ``/bin/sh -c`` command string. The latter is what makes
#: cancellation actually work: ``pkill -f`` matches ``/proc/PID/cmdline`` (the
#: argv), and the prefix puts the unique id into the shell's argv, so the kill
#: targets exactly this exec's process and nothing else.
_EXEC_ID_ENV = "JARN_EXEC_ID"


def _wrap_command(exec_id: str, command: str) -> str:
    """Embed *exec_id* into the command's argv so ``pkill -f`` can match it.

    Returns a ``/bin/sh`` command string that runs *command* unchanged but is
    prefixed with a ``:`` (shell no-op) carrying ``JARN_EXEC_ID=<id>`` as a
    literal token. ``pkill -f`` matches against ``/proc/PID/cmdline``, which for
    ``/bin/sh -c '<this string>'`` contains the prefix verbatim — so a later
    ``pkill -9 -f JARN_EXEC_ID=<id>`` reliably finds and kills this exec's shell
    (and, with ``pkill``'s default, its descendants are reaped when the shell
    dies under ``--init``). Using a no-op ``:`` means the prefix has zero effect
    on the user command's behaviour or exit code.
    """
    return f": {_EXEC_ID_ENV}={exec_id} ; {command}"


def docker_available() -> bool:
    """Return True if the ``docker`` CLI is on PATH and the daemon is reachable.

    A reachable daemon matters: ``docker`` can be installed while the daemon is
    stopped, in which case every ``exec`` would fail. We probe with
    ``docker info`` (cheap, no side effects) and a short timeout.
    """
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            ["docker", "info"],  # noqa: S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_CONTROL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _run_dir() -> Path:
    """Directory holding per-session docker run-state pid-files."""
    from jarn.config import paths

    return paths.global_home() / "run"


def _pid_alive(pid: int) -> bool:
    """True if a process with *pid* is currently alive (POSIX signal-0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _reap_stale_containers() -> None:
    """Remove jarn containers orphaned by a *crashed* prior run — only those.

    Each session writes a pid-file ``<JARN_HOME>/run/<pid>.docker-session``
    containing its unique session id, and labels its container with that same
    ``--label jarn-session=<uuid>``. This reaper, called once before a new
    container starts, looks at every pid-file: if the owning process is **no
    longer alive**, its session id is stale, so any container still carrying that
    label is force-removed and the pid-file deleted.

    Crucially this is session-scoped: a *concurrent* jarn session in a sibling
    process has a live pid, so its pid-file is skipped and its container is never
    touched (the prior global ``label=jarn=1`` reaper would have killed it). The
    reaper only ever removes containers whose owning process is dead.
    """
    run_dir = _run_dir()
    if not run_dir.is_dir():
        return
    for pid_file in run_dir.glob("*.docker-session"):
        try:
            pid = int(pid_file.stem)
        except ValueError:
            with contextlib.suppress(OSError):
                pid_file.unlink()
            continue
        if _pid_alive(pid):
            continue  # live (possibly concurrent) session — never touch it
        try:
            session_id = pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if session_id:
            _remove_containers_for_session(session_id)
        with contextlib.suppress(OSError):
            pid_file.unlink()


def _remove_containers_for_session(session_id: str) -> None:
    """Force-remove any running container carrying ``label=jarn-session=<id>``."""
    try:
        ps = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker", "ps", "-q",
                "--filter", f"label=jarn-session={session_id}",
            ],
            capture_output=True,
            text=True,
            timeout=_CONTROL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if ps.returncode != 0:
        return
    cids = ps.stdout.strip().split()
    if not cids:
        return
    logger.info("jarn: reaping %d stale jarn container(s): %s", len(cids), cids)
    for cid in cids:
        try:
            subprocess.run(  # noqa: S603
                ["docker", "rm", "-f", cid],  # noqa: S607
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CONTROL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("jarn: failed to reap stale container %s", cid)


class DockerStartError(RuntimeError):
    """Raised when the container cannot be started (image pull / daemon error)."""


class CancellableDockerSandbox(BaseSandbox):
    """Execution backend backed by a long-lived Docker container.

    Parameters
    ----------
    project_root:
        Host directory bind-mounted read-write into the container at the same
        absolute path. The agent's working directory.
    image:
        Container image. Must provide ``python3`` and ``/bin/sh``.
    allow_network:
        When False, the container is started with ``--network none`` — the agent
        gets no outbound network from inside the sandbox at all (defence in depth
        on top of the permission engine's NETWORK gating of web/MCP tools).
    extra_writable:
        Additional host paths bind-mounted read-write (e.g. shared caches).
    default_timeout:
        Per-command timeout when a tool call does not specify one.
    memory:
        Docker ``--memory`` value (e.g. ``"2g"``). Empty string = no cap.
    pids:
        Docker ``--pids-limit`` value. 0 = no cap.
    cpus:
        Docker ``--cpus`` value (e.g. ``"2"``). Empty string = no cap.
    user:
        Docker ``--user`` value (e.g. ``"1000:1000"``). Empty string = image
        default (typically root). See schema comment for the Linux footgun.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        image: str = DEFAULT_IMAGE,
        allow_network: bool = True,
        extra_writable: list[Path] | None = None,
        default_timeout: int = 30 * 60,
        memory: str = "",
        pids: int = 0,
        cpus: str = "",
        user: str = "",
    ) -> None:
        self._root = Path(project_root).resolve()
        self._image = image
        self._allow_network = allow_network
        self._extra_writable = [Path(p).resolve() for p in (extra_writable or [])]
        self._default_timeout = default_timeout
        self._memory = memory
        self._pids = pids
        self._cpus = cpus
        self._user = user
        self._live: set[subprocess.Popen] = set()
        self._live_lock = threading.Lock()
        self._container_id: str | None = None
        # Maps exec_id → client Popen; used by terminate_all() to kill in-container.
        self._live_exec_ids: dict[str, subprocess.Popen] = {}
        self._live_exec_lock = threading.Lock()
        # Per-session unique id: stamped on the container as a label and recorded
        # in a pid-file so the anti-orphan reaper can target ONLY this session's
        # crashed remains, never a concurrent sibling session's live container.
        self._session_id = uuid.uuid4().hex
        self._pid_file: Path | None = None
        # Reap containers orphaned by a crashed *prior* run before starting a new
        # one (scoped by pid-file liveness, so concurrent sessions are untouched).
        _reap_stale_containers()
        self._write_pid_file()
        self._start()

    def _write_pid_file(self) -> None:
        """Record this session's id under ``<JARN_HOME>/run/<pid>.docker-session``."""
        try:
            run_dir = _run_dir()
            run_dir.mkdir(parents=True, exist_ok=True)
            pid_file = run_dir / f"{os.getpid()}.docker-session"
            pid_file.write_text(self._session_id, encoding="utf-8")
            self._pid_file = pid_file
        except OSError:
            self._pid_file = None  # best-effort; reaping is a convenience

    # -- lifecycle ---------------------------------------------------------

    def _run_argv(self) -> list[str]:
        """Build the ``docker run`` argv for the session container."""
        argv = [
            "docker", "run", "-d", "--rm", "--init",
            "--label", "jarn=1",
            "--label", f"jarn-session={self._session_id}",
            "-v", f"{self._root}:{self._root}",
            "-w", str(self._root),
        ]
        for p in self._extra_writable:
            argv.extend(["-v", f"{p}:{p}"])
        if not self._allow_network:
            argv.extend(["--network", "none"])
        if self._memory:
            argv.extend(["--memory", self._memory])
        if self._pids:
            argv.extend(["--pids-limit", str(self._pids)])
        if self._cpus:
            argv.extend(["--cpus", self._cpus])
        if self._user:
            argv.extend(["--user", self._user])
        argv.extend([self._image, "sleep", "infinity"])
        return argv

    def _start(self) -> None:
        try:
            proc = subprocess.run(  # noqa: S603
                self._run_argv(),
                capture_output=True,
                text=True,
                timeout=_CONTROL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerStartError(f"docker run failed: {exc}") from exc
        if proc.returncode != 0:
            raise DockerStartError(
                f"docker run failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        self._container_id = proc.stdout.strip()
        logger.info(
            "jarn: started docker sandbox %s (image=%s, network=%s)",
            self._container_id[:12], self._image,
            "on" if self._allow_network else "none",
        )

    def close(self) -> None:
        """Force-remove the session container + pid-file (idempotent)."""
        if self._pid_file is not None:
            with contextlib.suppress(OSError):
                self._pid_file.unlink()
            self._pid_file = None
        cid = self._container_id
        if cid is None:
            return
        self._container_id = None
        try:
            subprocess.run(  # noqa: S603
                ["docker", "rm", "-f", cid],  # noqa: S607
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CONTROL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("jarn: failed to remove docker sandbox %s", cid[:12])

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        with contextlib.suppress(Exception):
            self.close()

    # -- BaseSandbox abstract surface --------------------------------------

    @property
    def id(self) -> str:
        return self._container_id or "<stopped>"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1, truncated=False,
            )
        if self._container_id is None:
            return ExecuteResponse(
                output="Error: docker sandbox is not running.",
                exit_code=126, truncated=False,
            )
        effective_timeout = timeout if timeout is not None else self._default_timeout

        # Each exec gets a unique ID. It is BOTH passed as an env var (so child
        # processes inherit it) AND embedded as a literal token in the /bin/sh
        # command string via _wrap_command(). The embedded token is what lets
        # terminate_all() actually kill this exec: pkill -f matches the shell's
        # /proc/PID/cmdline (its argv), which now carries JARN_EXEC_ID=<id>.
        exec_id = uuid.uuid4().hex
        wrapped = _wrap_command(exec_id, command)

        argv = [
            "docker", "exec",
            "-e", f"{_EXEC_ID_ENV}={exec_id}",
            "-w", str(self._root),
            self._container_id, "/bin/sh", "-c", wrapped,
        ]
        proc = subprocess.Popen(  # noqa: S603
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        with self._live_lock:
            self._live.add(proc)
        with self._live_exec_lock:
            self._live_exec_ids[exec_id] = proc
        try:
            try:
                stdout, stderr = proc.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                self._kill_in_container(exec_id)
                proc.communicate()
                return ExecuteResponse(
                    output=f"Error: Command timed out after {effective_timeout} seconds.",
                    exit_code=124, truncated=False,
                )
        finally:
            with self._live_lock:
                self._live.discard(proc)
            with self._live_exec_lock:
                self._live_exec_ids.pop(exec_id, None)

        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.extend(f"[stderr] {line}" for line in stderr.strip().split("\n"))
        output = "\n".join(parts) if parts else "<no output>"
        code = proc.returncode or 0
        if code != 0:
            output = f"{output.rstrip()}\n\nExit code: {code}"
        return ExecuteResponse(output=output, exit_code=code, truncated=False)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Write each (path, bytes) into the container via piped stdin.

        Bytes are streamed to ``cat`` over ``docker exec -i`` so binary content
        and arbitrary sizes are safe (no ARG_MAX, no shell-escaping the body).
        Only the destination path is shell-quoted into the command.
        """
        results: list[FileUploadResponse] = []
        for path, data in files:
            results.append(self._upload_one(path, data))
        return results

    def _upload_one(self, path: str, data: bytes) -> FileUploadResponse:
        # ``error`` is a standardized FileOperationError code (matching the
        # deepagents backend convention); the descriptive detail is logged.
        if self._container_id is None:
            logger.warning("jarn docker upload %s: sandbox not running", path)
            return FileUploadResponse(path=path, error="permission_denied")
        q = shlex.quote(path)
        shell_cmd = f"mkdir -p \"$(dirname {q})\" && cat > {q}"
        argv = [
            "docker", "exec", "-i", "-w", str(self._root),
            self._container_id, "/bin/sh", "-c", shell_cmd,
        ]
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                input=data,
                capture_output=True,
                timeout=_CONTROL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("jarn docker upload %s failed: %s", path, exc)
            return FileUploadResponse(path=path, error="permission_denied")
        if proc.returncode != 0:
            logger.warning(
                "jarn docker upload %s failed: %s",
                path, proc.stderr.decode("utf-8", "replace").strip(),
            )
            return FileUploadResponse(path=path, error="permission_denied")
        return FileUploadResponse(path=path)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read each path's bytes out of the container via ``cat``."""
        results: list[FileDownloadResponse] = []
        for path in paths:
            results.append(self._download_one(path))
        return results

    def _download_one(self, path: str) -> FileDownloadResponse:
        if self._container_id is None:
            return FileDownloadResponse(
                path=path, content=None, error="permission_denied"
            )
        q = shlex.quote(path)
        argv = [
            "docker", "exec", "-w", str(self._root),
            self._container_id, "/bin/sh", "-c", f"cat {q}",
        ]
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                timeout=_CONTROL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("jarn docker download %s failed: %s", path, exc)
            return FileDownloadResponse(path=path, content=None, error="permission_denied")
        if proc.returncode != 0:
            return FileDownloadResponse(path=path, content=None, error="file_not_found")
        return FileDownloadResponse(path=path, content=proc.stdout, error=None)

    # -- cancellation ------------------------------------------------------

    def _kill(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        terminate_process_group(proc.pid)

    def _kill_in_container(self, exec_id: str) -> None:
        """Kill the in-container shell(s) carrying *exec_id* in their argv.

        Uses ``pkill -9 -f JARN_EXEC_ID=<exec_id>`` to match the ``/bin/sh -c``
        process whose command line carries the ``: JARN_EXEC_ID=<id> ;`` no-op
        prefix injected by :func:`_wrap_command`. ``pkill -f`` matches against
        ``/proc/PID/cmdline`` (argv), and the prefix is in the shell's argv, so
        this targets precisely the shell started by this execute() call. When the
        shell dies under ``--init``, its child process group is reaped too. The
        pattern is anchored on ``JARN_EXEC_ID=`` so it cannot match a bare
        substring of unrelated commands.

        Requires ``pkill`` (``procps``) in the image; the default ``python:3.12``
        ships it. On a slim image lacking pkill the call is a silent no-op and
        cancellation falls back to client-death propagation via the exec shim.
        """
        cid = self._container_id
        if cid is None:
            return
        pattern = f"{_EXEC_ID_ENV}={exec_id}"
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "docker", "exec", cid,
                    "/bin/sh", "-c",
                    f"pkill -9 -f {shlex.quote(pattern)} 2>/dev/null || true",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CONTROL_TIMEOUT,
                check=False,
            )

    def terminate_all(self) -> int:
        """Kill in-flight ``docker exec`` clients on turn cancel.

        Does NOT remove the container — the session continues; only the
        currently-running command(s) are interrupted. Returns the count.

        Two-step cancel: first kill the client-side ``docker exec`` Popen (stops
        output collection and frees the fd), then issue a ``docker exec pkill``
        into the container to kill the actual in-container process. Both steps
        are best-effort; a failure in the in-container kill leaves only the
        container process running (the client is already dead), which is the same
        as the pre-hardening behaviour.
        """
        with self._live_lock:
            procs = list(self._live)
        with self._live_exec_lock:
            exec_ids = list(self._live_exec_ids.keys())
        for proc in procs:
            self._kill(proc)
        for exec_id in exec_ids:
            self._kill_in_container(exec_id)
        return len(procs)
