"""Docker backend integration tests — real daemon, no mocks (M1.4).

These tests exercise the actual isolation guarantees of
:class:`~jarn.agent.docker_backend.CancellableDockerSandbox` against a live
Docker daemon.  The entire module is skipped when no daemon is present so it
is safe to run in CI environments or on dev boxes without Docker.

Skip mechanism: module-level ``pytestmark`` with ``skipif`` on
``docker_available()`` — pytest collects all items then skips them with a
clean "docker daemon not available" reason rather than erroring.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from jarn.agent.docker_backend import CancellableDockerSandbox, docker_available

# ---------------------------------------------------------------------------
# Module-level skip guard — whole file skips cleanly when daemon is absent.
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.skipif(
        not docker_available(),
        reason="docker daemon not available",
    ),
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="docker integration tests run on Linux/macOS CI only",
    ),
]

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

#: Lightweight image for pure shell / isolation tests (no python3 needed).
_ALPINE = "alpine"

#: Image for file-operation tests (BaseSandbox derives helpers via python3).
_PYTHON = "python:3.12-slim"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def alpine_sandbox(tmp_path: Path):
    """Session-scoped-like function fixture: alpine sandbox over a tmp project root."""
    root = tmp_path / "proj"
    root.mkdir()
    sb = CancellableDockerSandbox(project_root=root, image=_ALPINE)
    try:
        yield sb, root
    finally:
        sb.close()


@pytest.fixture
def python_sandbox(tmp_path: Path):
    """Function fixture: python:3.12-slim sandbox over a tmp project root."""
    root = tmp_path / "proj"
    root.mkdir()
    sb = CancellableDockerSandbox(project_root=root, image=_PYTHON)
    try:
        yield sb, root
    finally:
        sb.close()


@pytest.fixture
def no_network_sandbox(tmp_path: Path):
    """Function fixture: alpine sandbox with allow_network=False."""
    root = tmp_path / "proj"
    root.mkdir()
    sb = CancellableDockerSandbox(
        project_root=root, image=_PYTHON, allow_network=False
    )
    try:
        yield sb, root
    finally:
        sb.close()


@pytest.fixture
def network_sandbox(tmp_path: Path):
    """Function fixture: python sandbox with allow_network=True."""
    root = tmp_path / "proj"
    root.mkdir()
    sb = CancellableDockerSandbox(
        project_root=root, image=_PYTHON, allow_network=True
    )
    try:
        yield sb, root
    finally:
        sb.close()


# ---------------------------------------------------------------------------
# Invariant 1: basic execute + container id
# ---------------------------------------------------------------------------


def test_container_id_is_real_and_echo_works(alpine_sandbox):
    """backend.id is a real container id and execute('echo hi') succeeds."""
    sb, _root = alpine_sandbox
    # Container id should look like a full 64-char hex or at least non-trivial.
    assert sb.id and sb.id != "<stopped>", f"Expected a real container id, got {sb.id!r}"
    assert len(sb.id) >= 12, f"Container id looks too short: {sb.id!r}"

    resp = sb.execute("echo hi")
    assert resp.exit_code == 0, f"Expected exit 0, got {resp.exit_code}: {resp.output}"
    assert "hi" in resp.output


# ---------------------------------------------------------------------------
# Invariant 2: project-root bind-mount round-trips to host
# ---------------------------------------------------------------------------


def test_project_root_write_appears_on_host(alpine_sandbox):
    """A file written inside the container's project-root path appears on the host."""
    sb, root = alpine_sandbox
    target = root / "hello.txt"
    assert not target.exists(), "Precondition: file must not exist yet"

    # Write via shell inside the container (project root is the working dir).
    resp = sb.execute("echo 'from-container' > hello.txt")
    assert resp.exit_code == 0, resp.output

    # The bind-mount maps the host path at the same absolute path, so the file
    # must now appear on the host at exactly root/hello.txt.
    assert target.exists(), "File written in container should appear on host via bind-mount"
    content = target.read_text().strip()
    assert content == "from-container", f"Unexpected content: {content!r}"


def test_upload_then_download_round_trips(python_sandbox):
    """upload_files → download_files round-trips bytes correctly."""
    sb, _root = python_sandbox
    payload = b"binary\x00data\xff\n"
    path = str(_root / "data.bin")

    up = sb.upload_files([(path, payload)])
    assert up[0].error is None, f"Upload failed: {up[0].error}"

    dn = sb.download_files([path])
    assert dn[0].error is None, f"Download failed: {dn[0].error}"
    assert dn[0].content == payload


# ---------------------------------------------------------------------------
# Invariant 3: writes outside project root do NOT affect the host
# ---------------------------------------------------------------------------


def test_write_outside_project_root_does_not_affect_host(alpine_sandbox):
    """A command writing to /tmp/escape.txt inside the container must NOT create that path on the host."""
    sb, _root = alpine_sandbox
    host_path = Path("/tmp/jarn_integration_escape_test.txt")  # noqa: S108

    # Ensure it doesn't already exist from a prior run.
    if host_path.exists():
        host_path.unlink()

    # Write to /tmp inside the container — that's the container's own /tmp,
    # not a bind-mounted path, so the host filesystem is untouched.
    resp = sb.execute(f"echo 'should-not-escape' > {host_path}")
    assert resp.exit_code == 0, resp.output

    assert not host_path.exists(), (
        f"Container write to {host_path} leaked to host — bind-mount is too wide"
    )


# ---------------------------------------------------------------------------
# Invariant 4: network isolation
# ---------------------------------------------------------------------------


def _host_has_internet() -> bool:
    """Probe whether the host machine itself has outbound internet access."""
    try:
        socket.setdefaulttimeout(3)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except OSError:
        return False


def test_network_denied_when_allow_network_false(no_network_sandbox):
    """With allow_network=False, outbound network attempts fail (non-zero exit)."""
    sb, _root = no_network_sandbox
    # Try to connect outbound; with --network none this must fail.
    resp = sb.execute(
        "python3 -c \""
        "import urllib.request, sys; "
        "urllib.request.urlopen('http://example.com', timeout=5); "
        "print('CONNECTED')"
        "\""
    )
    assert resp.exit_code != 0, (
        f"Expected network-denied failure, but got exit 0. output: {resp.output!r}"
    )
    assert "CONNECTED" not in resp.output, (
        "Sandbox with allow_network=False should not reach example.com"
    )


@pytest.mark.skipif(
    not _host_has_internet(),
    reason="host has no internet access — cannot verify network-allowed path",
)
def test_network_allowed_when_allow_network_true(network_sandbox):
    """With allow_network=True and a live internet connection, outbound DNS/TCP works."""
    sb, _root = network_sandbox
    # Use a simple DNS-level probe: python3 socket.getaddrinfo is cheaper than
    # a full HTTP request and less flaky on slow connections.
    resp = sb.execute(
        "python3 -c \""
        "import socket; "
        "addrs = socket.getaddrinfo('example.com', 80); "
        "print('RESOLVED', len(addrs))"
        "\""
    )
    assert resp.exit_code == 0, f"DNS resolution failed: {resp.output!r}"
    assert "RESOLVED" in resp.output


# ---------------------------------------------------------------------------
# Invariant 5: terminate_all() interrupts a running command without removing the container
# ---------------------------------------------------------------------------


def test_terminate_all_interrupts_sleep_and_container_survives(alpine_sandbox):
    """terminate_all() kills in-flight execs; the container itself stays alive."""
    import threading

    sb, _root = alpine_sandbox

    results: list = []

    def _run_sleep():
        # sleep 60 should be interrupted well before completion.
        results.append(sb.execute("sleep 60", timeout=10))

    t = threading.Thread(target=_run_sleep, daemon=True)
    t.start()

    # Give the exec a moment to register in _live before we cancel.
    import time
    time.sleep(0.5)

    count = sb.terminate_all()
    t.join(timeout=12)

    assert not t.is_alive(), "Thread should have finished after terminate_all()"
    # The container must still be alive (id unchanged, not '<stopped>').
    assert sb.id and sb.id != "<stopped>", "Container should survive terminate_all()"
    # A subsequent exec must succeed — the container is still running.
    followup = sb.execute("echo still-alive")
    assert followup.exit_code == 0
    assert "still-alive" in followup.output
    _ = count  # count may be 0 if the thread's Popen was already reaped; that's fine


def test_terminate_all_actually_kills_container_process(alpine_sandbox):
    """terminate_all() must leave NO in-container process from the cancelled exec.

    This is the regression test for the cancellation-isolation blocker: it is not
    enough that the client thread unblocks — the container-side process tree the
    exec spawned must actually be dead. We spin a uniquely-named marker process,
    cancel it, then assert (from a fresh exec) that no process carrying that
    marker survives. ``pkill -f JARN_EXEC_ID=<id>`` (busybox on alpine) is what
    makes this pass.
    """
    import threading
    import time

    sb, _root = alpine_sandbox

    # A long-lived, uniquely-greppable command. The marker token is distinctive
    # so we can detect any surviving copy via ps from a separate exec.
    marker = "jarn_cancel_marker_zzz"
    started = threading.Event()

    def _run_marker():
        started.set()
        sb.execute(f"sleep 120 # {marker}", timeout=30)

    t = threading.Thread(target=_run_marker, daemon=True)
    t.start()
    started.wait(timeout=5)
    time.sleep(1.0)  # let the exec register and the sleep actually start

    sb.terminate_all()
    t.join(timeout=10)

    # Give the kernel a beat to reap the killed tree.
    time.sleep(0.5)

    # From a brand-new exec, count surviving processes carrying the marker. The
    # grep itself would match its own argv, so exclude the grep line.
    probe = sb.execute(
        f"ps -ef 2>/dev/null | grep {marker} | grep -v grep | wc -l"
    )
    assert probe.exit_code == 0, probe.output
    surviving = int(probe.output.strip().splitlines()[0])
    assert surviving == 0, (
        f"terminate_all() left {surviving} container-side process(es) carrying "
        f"the marker alive — cancellation did not actually kill the process tree"
    )
