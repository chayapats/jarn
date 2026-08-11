"""T-OPS-2: ``jarn gateway`` CLI entry + mocked-aiogram smoke test."""

from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarn.config.schema import Config, GatewayConfig, GatewayTelegramConfig
from jarn.telegram.backend import InMemoryGatewayBackend, SessionRouterBackend
from jarn.telegram.bot import TelegramBotApp
from jarn.telegram.outbox import Outbox

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_aiogram(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "aiogram" or name.startswith("aiogram."):
            raise ImportError("No module named 'aiogram'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)
    sys.modules.pop("aiogram", None)


def _gateway_config(
    *,
    enabled: bool = True,
    token: str = "test-token",
    allowed: list[int] | None = None,
) -> Config:
    cfg = Config()
    cfg.gateway = GatewayConfig(
        enabled=enabled,
        telegram=GatewayTelegramConfig(
            token=token,
            allowed_user_ids=list(allowed if allowed is not None else [42]),
        ),
    )
    return cfg


def _write_global_config(home: Path, *, body: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _assert_blocking_error(stderr: str, code: str) -> None:
    assert stderr.startswith(f"{code}:")
    for label in ("Cause:", "Component:", "retryable:", "Next:", "Log:"):
        assert label in stderr


@dataclass
class FakeBot:
    """Minimal aiogram Bot stand-in (no network)."""

    updates_pages: list[list[Any]] = field(default_factory=list)
    sent: list[tuple[Any, ...]] = field(default_factory=list)
    webhook_url: str = ""
    pending_update_count: int = 0
    _page: int = 0

    async def get_webhook_info(self):
        return SimpleNamespace(url=self.webhook_url, pending_update_count=self.pending_update_count)

    async def get_updates(self, offset=None, timeout=0, limit=100, allowed_updates=None):
        if self._page >= len(self.updates_pages):
            return []
        page = self.updates_pages[self._page]
        self._page += 1
        return page

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_message_draft(self, chat_id, draft_id, text=None, **kwargs):
        return True

    async def answer_callback_query(self, callback_query_id, **kwargs):
        return True

    @property
    def session(self):
        class _S:
            async def close(self_inner):
                return None

        return _S()


def _update_message(*, uid: int, user_id: int, chat_id: int, text: str, chat_type="private"):
    return SimpleNamespace(
        update_id=uid,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            from_user=SimpleNamespace(id=user_id),
            text=text,
            caption=None,
            photo=None,
            document=None,
            voice=None,
            audio=None,
            video=None,
            video_note=None,
            animation=None,
            sticker=None,
        ),
        edited_message=None,
        callback_query=None,
    )


# ---------------------------------------------------------------------------
# Parser / help
# ---------------------------------------------------------------------------


def test_build_parser_has_gateway_subcommand():
    from jarn.cli import build_parser

    parser = build_parser()
    help_text = parser.format_help()
    assert "gateway" in help_text

    args = parser.parse_args(["gateway", "--fake-backend"])
    assert args.command == "gateway"
    assert args.fake_backend is True


def test_gateway_help_mentions_fake_backend():
    from jarn.cli import build_parser

    parser = build_parser()
    gateway = None
    for action in parser._actions:
        if getattr(action, "dest", None) == "command":
            gateway = action.choices.get("gateway")
            break
    assert gateway is not None
    sub_help = gateway.format_help()
    assert "fake-backend" in sub_help


# ---------------------------------------------------------------------------
# Config / extra guards
# ---------------------------------------------------------------------------


def test_missing_telegram_extra_exits_clearly(monkeypatch, capsys):
    _block_aiogram(monkeypatch)
    from jarn.telegram.cli import load_gateway_settings

    with pytest.raises(SystemExit) as excinfo:
        load_gateway_settings(config=_gateway_config(), env={})
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    _assert_blocking_error(err, "JARN-GATEWAY-001")
    assert "jarn[telegram]" in err
    assert "configured-but-uninstalled" in err


def test_gateway_disabled_refuses(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "aiogram", SimpleNamespace(__name__="aiogram"))
    from jarn.telegram.cli import load_gateway_settings

    with pytest.raises(SystemExit) as excinfo:
        load_gateway_settings(
            config=_gateway_config(enabled=False),
            env={},
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    _assert_blocking_error(err, "JARN-GATEWAY-002")
    assert "gateway.enabled is false" in err


def test_missing_token_refuses(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "aiogram", SimpleNamespace(__name__="aiogram"))
    from jarn.telegram.cli import load_gateway_settings

    with pytest.raises(SystemExit) as excinfo:
        load_gateway_settings(
            config=_gateway_config(token=""),
            env={},
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    _assert_blocking_error(err, "JARN-GATEWAY-003")
    assert "Missing bot token" in err


def test_empty_allowlist_refuses(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "aiogram", SimpleNamespace(__name__="aiogram"))
    from jarn.telegram.cli import load_gateway_settings

    with pytest.raises(SystemExit) as excinfo:
        load_gateway_settings(
            config=_gateway_config(allowed=[]),
            env={},
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    _assert_blocking_error(err, "JARN-GATEWAY-004")
    assert "allowed_user_ids is empty" in err


def test_invalid_env_allowlist_has_stable_redacted_anatomy(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "aiogram", SimpleNamespace(__name__="aiogram"))
    from jarn.telegram.cli import load_gateway_settings

    with pytest.raises(SystemExit) as excinfo:
        load_gateway_settings(
            config=_gateway_config(),
            env={"JARN_TELEGRAM_ALLOWED_USER_IDS": "42,not-a-user"},
        )

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    _assert_blocking_error(err, "JARN-GATEWAY-004")
    assert "not-a-user" not in err


def test_unresolvable_token_has_stable_redacted_anatomy(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "aiogram", SimpleNamespace(__name__="aiogram"))
    from jarn.config.secrets import SecretResolutionError
    from jarn.telegram.cli import load_gateway_settings

    def _fail_resolve(_reference: str) -> str:
        raise SecretResolutionError("backend rejected sk-abcdefghijklmnopqrst")

    monkeypatch.setattr("jarn.config.secrets.resolve", _fail_resolve)
    with pytest.raises(SystemExit) as excinfo:
        load_gateway_settings(
            config=_gateway_config(token="keychain:jarn/telegram"),
            env={},
        )

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    _assert_blocking_error(err, "JARN-GATEWAY-003")
    assert "abcdefghijklmnopqrst" not in err


def test_corrupt_config_has_stable_redacted_anatomy(monkeypatch, capsys):
    def _fail_config():
        raise ValueError("invalid yaml with sk-abcdefghijklmnopqrst")

    monkeypatch.setattr("jarn.config.loader.load_config", _fail_config)
    from jarn.telegram.cli import run_gateway_cli

    assert run_gateway_cli(fake_backend=True) == 2
    err = capsys.readouterr().err
    _assert_blocking_error(err, "JARN-GATEWAY-002")
    assert "abcdefghijklmnopqrst" not in err


def test_load_settings_env_overrides(monkeypatch):
    monkeypatch.setitem(sys.modules, "aiogram", SimpleNamespace(__name__="aiogram"))
    from jarn.telegram.cli import load_gateway_settings

    settings = load_gateway_settings(
        config=_gateway_config(token="cfg-token", allowed=[1]),
        env={
            "JARN_TELEGRAM_BOT_TOKEN": "env-token",
            "JARN_TELEGRAM_ALLOWED_USER_IDS": "7,8",
            "JARN_TELEGRAM_FAKE_BACKEND": "1",
        },
    )
    assert settings.token == "env-token"
    assert settings.allowed_user_ids == [7, 8]
    assert settings.fake_backend is True


# ---------------------------------------------------------------------------
# Backend wiring
# ---------------------------------------------------------------------------


def test_build_backend_fake():
    from jarn.telegram.cli import build_backend

    backend, supervisor = build_backend(fake_backend=True)
    assert isinstance(backend, InMemoryGatewayBackend)
    assert supervisor is None


def test_build_backend_session_router(tmp_path, monkeypatch):
    from jarn.telegram.cli import build_backend

    home = tmp_path / "home"
    personal = home / "personal"
    personal.mkdir(parents=True)
    (personal / ".git").mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))

    backend, supervisor = build_backend(fake_backend=False)
    try:
        assert isinstance(backend, SessionRouterBackend)
        assert supervisor is not None
    finally:
        if supervisor is not None:
            supervisor.shutdown()


def test_run_gateway_cli_wires_fake_backend(monkeypatch, tmp_path):
    """Boot path: config → fake backend → run_gateway_bot (no network)."""
    from jarn.config import paths
    from jarn.telegram.backend import InMemoryGatewayBackend

    home = tmp_path / "home"
    _write_global_config(
        home,
        body=(
            "providers:\n"
                "  openrouter:\n"
                "    type: openrouter\n"
                "    api_key: ${TEST_OPENROUTER_KEY}\n"
            "gateway:\n"
            "  enabled: true\n"
            "  telegram:\n"
                "    token: ${TEST_TELEGRAM_TOKEN}\n"
            "    allowed_user_ids: [42]\n"
        ),
    )
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "sk-test")
    monkeypatch.setenv("TEST_TELEGRAM_TOKEN", "fake-token")
    monkeypatch.setenv("JARN_HOME", str(home))
    monkeypatch.setattr(paths, "global_config_path", lambda: home / "config.yaml")
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "aiogram", SimpleNamespace(__name__="aiogram"))

    captured: dict[str, Any] = {}

    async def _fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("jarn.telegram.bot.run_gateway_bot", _fake_run)

    from jarn.telegram.cli import run_gateway_cli

    code = run_gateway_cli(fake_backend=True)
    assert code == 0
    assert captured["token"] == "fake-token"
    assert captured["allowed_user_ids"] == [42]
    assert isinstance(captured["backend"], InMemoryGatewayBackend)


def test_cmd_gateway_dispatches_from_main_cli(monkeypatch, tmp_path):
    called: dict[str, Any] = {}

    def _stub(*, fake_backend: bool = False):
        called["fake_backend"] = fake_backend
        return 0

    monkeypatch.setattr("jarn.telegram.cli.run_gateway_cli", _stub)

    from jarn.cli import main

    assert main(["gateway", "--fake-backend"]) == 0
    assert called["fake_backend"] is True


def test_module_entry_parses_fake_backend(monkeypatch):
    called: dict[str, Any] = {}

    def _stub(*, fake_backend: bool = False, env=None):
        called["fake_backend"] = fake_backend
        return 0

    monkeypatch.setattr("jarn.telegram.cli.run_gateway_cli", _stub)
    from jarn.telegram.cli import main as tg_main

    assert tg_main(["--fake-backend"]) == 0
    assert called["fake_backend"] is True


# ---------------------------------------------------------------------------
# Integration smoke: mocked aiogram — boot, auth reject, synthetic DM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_boot_auth_reject_and_synthetic_dm(monkeypatch, tmp_path):
    """Boot-shaped path: settings + fake backend; reject stranger; DM → submit_turn."""
    from jarn.config import paths
    from jarn.telegram.bot import drain_backlog
    from jarn.telegram.cli import build_backend, load_gateway_settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setitem(sys.modules, "aiogram", SimpleNamespace(__name__="aiogram"))

    settings = load_gateway_settings(
        fake_backend=True,
        config=_gateway_config(token="fake", allowed=[42]),
        env={},
    )
    backend, supervisor = build_backend(fake_backend=settings.fake_backend)
    assert supervisor is None
    assert isinstance(backend, InMemoryGatewayBackend)

    # Backlog drain (mocked Bot — no network); must not execute.
    backlog_bot = FakeBot(
        updates_pages=[[_update_message(uid=1, user_id=42, chat_id=42, text="stale backlog")]]
    )
    report = await drain_backlog(backlog_bot)
    assert report.count == 1
    assert backend.turns == []

    app = TelegramBotApp(
        token=settings.token,
        allowed_user_ids=settings.allowed_user_ids,
        backend=backend,
    )
    app._bot = None  # skip media download path
    app._outbox = Outbox(sender=FakeBot())
    app._offset = report.offset

    # Auth reject
    await app.handle_update(_update_message(uid=2, user_id=99, chat_id=99, text="nope"))
    assert backend.turns == []

    # Synthetic allowed DM → backend.submit_turn
    await app.handle_update(_update_message(uid=3, user_id=42, chat_id=42, text="hello from smoke"))
    turn = backend.last_turn()
    assert turn is not None
    assert turn.chat_id == 42
    assert turn.user_id == 42
    assert turn.text == "hello from smoke"
