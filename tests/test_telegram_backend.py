"""SessionRouterBackend adapter coverage (production Telegram→daemon path)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarn.config.schema import GatewayRepo
from jarn.gateway.daemon import DaemonSupervisor
from jarn.gateway.protocol import MediaRef, TurnFrame
from jarn.gateway.sessions import SessionRouter
from jarn.telegram.backend import SessionRouterBackend


@pytest.mark.asyncio
async def test_submit_verdict_routes_to_active_root(tmp_path: Path, monkeypatch):
    """Callback verdicts must hit supervisor.send_approval_verdict on active root."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))

    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / ".jarn").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".jarn").mkdir()

    monkeypatch.setattr(
        "jarn.config.paths.ensure_personal_root", lambda: personal
    )

    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    router = SessionRouter(
        supervisor,
        repos=[GatewayRepo(path=str(repo), name="app")],
        personal_root=personal,
    )
    backend = SessionRouterBackend(router=router, supervisor=supervisor)

    await backend.set_repo(chat_id=42, user_id=1, name_or_path="app")
    await backend.submit_verdict(
        chat_id=42,
        user_id=1,
        token="tok-1",
        approved=True,
        scope="session",
        plan_mode_target="auto-edit",
        message="go",
        kind="tool",
    )

    supervisor.send_approval_verdict.assert_called_once_with(
        repo.resolve(),
        token="tok-1",
        approved=True,
        scope="session",
        message="go",
        plan_mode_target="auto-edit",
    )


@pytest.mark.asyncio
async def test_submit_turn_forwards_media_refs(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / ".jarn").mkdir()

    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    sent: list = []

    def _send(root, frame):
        sent.append((root, frame))

    supervisor.send.side_effect = _send
    supervisor.ensure_worker.return_value = SimpleNamespace(root=personal.resolve())
    supervisor.get_worker.return_value = None

    router = SessionRouter(supervisor, personal_root=personal)
    backend = SessionRouterBackend(router=router, supervisor=supervisor)
    media = [MediaRef(path="/tmp/x.png", mime="image/png", modality="image")]
    await backend.submit_turn(
        chat_id=7, user_id=1, text="see this", media=media
    )

    assert sent
    _root, frame = sent[0]
    assert _root == personal.resolve()
    assert isinstance(frame, TurnFrame)
    assert frame.text == "see this"
    assert frame.media and frame.media[0].path == "/tmp/x.png"
