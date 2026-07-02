"""Execution backend factories for the agent runtime."""

from __future__ import annotations

from pathlib import Path

from jarn.config.schema import Config


class SandboxUnavailable(RuntimeError):
    """Raised when a sandbox backend is requested but cannot be constructed."""


def _make_local_backend(project_root: Path | None, config: Config | None = None):
    """Local-first backend: real filesystem + shell, scoped to the project root.

    ``virtual_mode=True`` adds path guardrails (blocks ``..``/absolute escapes)
    for filesystem ops. Shell execution is still on the host — that is gated by
    the permission engine and danger-guard at the TUI layer.

    When ``config.execution.local_sandbox`` is ``"auto"`` or ``"require"``, each
    shell command is additionally wrapped by :mod:`jarn.agent.os_sandbox` so the
    kernel enforces write isolation and optional network denial.  The default is
    ``"off"`` which preserves the original behaviour exactly.
    """
    from jarn.agent.local_backend import CancellableLocalShellBackend

    root = str(project_root) if project_root else str(Path.cwd())
    root_path = Path(root)

    sandbox_mode = "off"
    sandbox_allow_network = True
    sandbox_extra_writable: list[Path] = []

    if config is not None:
        ex = config.execution
        sandbox_mode = ex.local_sandbox
        sandbox_allow_network = ex.sandbox_allow_network
        sandbox_extra_writable = [Path(p).expanduser() for p in ex.sandbox_writable]

    return CancellableLocalShellBackend(
        root_dir=root,
        virtual_mode=True,
        sandbox_mode=sandbox_mode,
        project_root=root_path,
        sandbox_allow_network=sandbox_allow_network,
        sandbox_extra_writable=sandbox_extra_writable,
    )


def _make_sandbox_backend(config: Config):
    """Construct an isolated sandbox backend.

    Sandbox execution requires an external runtime (e.g. a LangSmith Sandbox)
    and credentials; if unavailable we raise :class:`SandboxUnavailable` so the
    caller can fall back to local with a clear message.
    """
    provider = config.execution.sandbox_provider
    if provider == "langsmith":
        try:
            from langgraph_sandbox import Sandbox  # type: ignore
        except ImportError as exc:
            raise SandboxUnavailable(
                "LangSmith sandbox runtime not installed. Install the sandbox "
                "extra and set credentials, or use execution.backend: local."
            ) from exc
        try:
            from jarn.agent.sandbox_backend import CancellableLangSmithSandbox

            return CancellableLangSmithSandbox(Sandbox())
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailable(f"Could not start sandbox: {exc}") from exc
    raise SandboxUnavailable(f"Unknown sandbox provider: {provider!r}")


def _make_docker_backend(config: Config, project_root: Path | None):
    """Construct a Docker container backend (real OS-level isolation).

    Requires the ``docker`` CLI on PATH and a reachable daemon; otherwise raises
    :class:`SandboxUnavailable` so the caller can fail closed (or fall back to
    the host only when ``allow_local_fallback`` is set). The project root is
    bind-mounted read-write at its own absolute path; everything else the agent
    sees is the container image's filesystem.
    """
    from jarn.agent.docker_backend import (
        CancellableDockerSandbox,
        DockerStartError,
        docker_available,
    )

    if not docker_available():
        raise SandboxUnavailable(
            "Docker is not available (the `docker` CLI is missing or the daemon "
            "is not running). Start Docker, or use execution.backend: local."
        )
    root = Path(project_root) if project_root else Path.cwd()
    ex = config.execution
    try:
        return CancellableDockerSandbox(
            project_root=root,
            image=ex.docker_image,
            allow_network=ex.sandbox_allow_network,
            extra_writable=[Path(p).expanduser() for p in ex.sandbox_writable],
            memory=ex.docker_memory,
            pids=ex.docker_pids,
            cpus=ex.docker_cpus,
            user=ex.docker_user,
        )
    except DockerStartError as exc:
        raise SandboxUnavailable(
            f"Could not start docker sandbox with image {ex.docker_image!r}: {exc}\n"
            f"The image may not be present locally — try: docker pull {ex.docker_image}"
        ) from exc


def _make_backend(config: Config, project_root: Path | None):
    if config.execution.backend == "docker":
        return _make_docker_backend(config, project_root)  # may raise SandboxUnavailable
    if config.execution.backend == "sandbox":
        # The "sandbox" backend historically meant the remote LangSmith runtime;
        # `sandbox_provider: docker` redirects it to the local container backend.
        if config.execution.sandbox_provider == "docker":
            return _make_docker_backend(config, project_root)
        return _make_sandbox_backend(config)  # may raise SandboxUnavailable
    return _make_local_backend(project_root, config)
