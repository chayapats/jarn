"""Execution backend / sandbox toggle tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.agent.builder import SandboxUnavailable, _make_backend, _make_sandbox_backend
from jarn.config.loader import load_config


def test_execution_config_parsed(tmp_path):
    gp = tmp_path / "g.yaml"
    gp.write_text(yaml.safe_dump({"execution": {"backend": "sandbox"}}), encoding="utf-8")
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.execution.backend == "sandbox"


def test_default_execution_is_local():
    cfg = load_config(global_path=None, project_path=None)
    assert cfg.execution.backend == "local"


def test_make_local_backend(base_config, tmp_path):
    from deepagents.backends import LocalShellBackend

    backend = _make_backend(base_config, tmp_path)
    # Our cancellable subclass (kills spawned process trees on turn cancel).
    assert isinstance(backend, LocalShellBackend)
    assert backend.__class__.__name__ == "CancellableLocalShellBackend"


def test_sandbox_unavailable_raises(base_config):
    base_config.execution.backend = "sandbox"
    with pytest.raises(SandboxUnavailable):
        _make_backend(base_config, None)


def test_unknown_sandbox_provider(base_config):
    base_config.execution.sandbox_provider = "nope"
    with pytest.raises(SandboxUnavailable):
        _make_sandbox_backend(base_config)


def test_cancellable_langsmith_sandbox_terminate_all():
    from jarn.agent.sandbox_backend import CancellableLangSmithSandbox

    killed: list = []

    class _Handle:
        def __iter__(self):
            return iter(())

        @property
        def result(self):
            from types import SimpleNamespace

            return SimpleNamespace(stdout="ok", stderr="", exit_code=0)

        def kill(self):
            killed.append(self)

    class _Sandbox:
        name = "test-sandbox"

        def run(self, command, *, timeout, wait=False):
            assert wait is False
            return _Handle()

    backend = CancellableLangSmithSandbox(_Sandbox())
    resp = backend.execute("sleep 30")
    assert resp.exit_code == 0
    assert backend.terminate_all() == 0  # command already finished


@pytest.mark.asyncio
async def test_ensure_runtime_fails_closed_without_opt_in(tmp_path, monkeypatch, base_config):
    """Sandbox requested but unavailable → refuse to silently run on the host."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    base_config.execution.backend = "sandbox"  # allow_local_fallback defaults False

    from jarn.tui.controller import Controller

    ctrl = Controller(base_config, root)
    fake = GenericFakeChatModel(messages=iter([]))
    with (
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        pytest.raises(SandboxUnavailable),
    ):
        await ctrl.ensure_runtime()
    assert ctrl.health == "error"
    assert "Refusing to run on the host" in (ctrl.last_error or "")
    assert ctrl.config.execution.backend == "sandbox"  # NOT silently downgraded
    ctrl.close()


@pytest.mark.asyncio
async def test_ensure_runtime_falls_back_when_opted_in(tmp_path, monkeypatch, base_config):
    """With explicit opt-in, downgrade to host but mark the session degraded."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    base_config.execution.backend = "sandbox"
    base_config.execution.allow_local_fallback = True

    from jarn.tui.controller import Controller

    ctrl = Controller(base_config, root)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = await ctrl.ensure_runtime()
    assert rt is not None
    assert ctrl.config.execution.backend == "local"  # fell back (opted in)
    assert ctrl.health == "degraded"
    assert "host (no sandbox)" in ctrl.status_line  # visible, not silent
    ctrl.close()
