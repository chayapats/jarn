from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jarn.config.secrets import StoredSecret
from jarn.onboarding.credentials import ActivatedCredential
from jarn.telegram.setup import (
    GatewayServiceManager,
    ServiceStatus,
    TelegramBotIdentity,
    TelegramOperator,
    TelegramSetupClient,
    TelegramSetupFailure,
    run_gateway_service_install,
    run_gateway_setup,
    run_gateway_status,
)

_BASE_CONFIG = """\
# operator customization survives Telegram setup
config_version: 3
default_profile: codex_subscription
default_model: codex_subscription/gpt-5.4
providers:
  codex_subscription:
    type: codex_subscription
routing:
  main: codex_subscription/gpt-5.4
  subagent: codex_subscription/gpt-5.4
  summarizer: codex_subscription/gpt-5.4
  fallback: []
ui:
  theme: high-contrast
"""


class _Client:
    def __init__(self, operators: list[TelegramOperator] | None = None) -> None:
        self.operators = operators or [TelegramOperator(24680, "owner", "Jarn Owner")]
        self.prepared: list[str] = []
        self.discovered: list[tuple[str, int | None, float]] = []

    def prepare(self, token: str):
        self.prepared.append(token)
        return TelegramBotIdentity(99, "jarn_test_bot", "Jarn Test Bot"), 51

    def discover(self, token: str, *, offset: int | None, timeout_seconds: float):
        self.discovered.append((token, offset, timeout_seconds))
        return self.operators


class _NoService:
    unit_path = Path("/not-written/jarn-telegram.service")

    def status(self) -> ServiceStatus:
        return ServiceStatus(False, False, False, False, None)


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path = home / "config.yaml"
    path.write_text(_BASE_CONFIG, encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("JARN_HOME", str(home))
    return path


def _fake_credential() -> ActivatedCredential:
    return ActivatedCredential(
        provider="telegram",
        service="jarn",
        account="telegram.setup-test",
        stored=StoredSecret("keychain:jarn/telegram.setup-test", "keychain"),
    )


def test_gateway_setup_discovers_operator_and_commits_secret_reference(
    tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path, monkeypatch)
    token = "123456:top-secret-token"
    client = _Client()
    activated = _fake_credential()
    answers = iter(["", "yes"])
    monkeypatch.setattr(
        "jarn.telegram.setup.activate_pending_credential",
        lambda provider, value: activated,
    )
    monkeypatch.setattr(
        "jarn.telegram.setup.resolve",
        lambda reference: token if reference == activated.reference else None,
    )

    result = run_gateway_setup(
        client=client,
        service_manager=_NoService(),
        input_fn=lambda _prompt: next(answers),
        secret_reader=lambda _prompt: token,
    )

    assert result == 0
    assert client.prepared == [token]
    assert client.discovered == [(token, 51, 120.0)]
    raw_text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(raw_text)
    assert raw["gateway"] == {
        "enabled": True,
        "telegram": {
            "token": activated.reference,
            "allowed_user_ids": [24680],
        },
    }
    assert raw["ui"] == {"theme": "high-contrast"}
    assert "# operator customization survives" in raw_text
    output = capsys.readouterr()
    assert token not in output.out + output.err + raw_text
    assert "Verified bot: @jarn_test_bot" in output.out
    assert "Detected operator" in output.out
    assert "jarn gateway status" in output.out
    assert list(path.parent.glob("config.yaml.bak.*"))


def test_setup_client_verifies_bot_and_discovers_only_new_private_start(monkeypatch):
    closed: list[str] = []

    class _Session:
        def __init__(self, label: str) -> None:
            self.label = label

        async def close(self):
            closed.append(self.label)

    old_updates = [SimpleNamespace(update_id=value, message=None) for value in range(7, 107)]
    ignored_group = SimpleNamespace(
        update_id=107,
        message=SimpleNamespace(
            chat=SimpleNamespace(type="group"),
            from_user=SimpleNamespace(
                id=111, is_bot=False, username="group", full_name="Group User"
            ),
            text="/start",
        ),
    )
    new_start = SimpleNamespace(
        update_id=108,
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            from_user=SimpleNamespace(
                id=24680,
                is_bot=False,
                username="owner",
                full_name="Jarn Owner",
            ),
            text="/start payload",
        ),
    )

    class _PrepareBot:
        session = _Session("prepare")
        pages = 0

        async def get_me(self):
            return SimpleNamespace(id=99, username="jarn_bot", full_name="Jarn Bot")

        async def get_webhook_info(self):
            return SimpleNamespace(url="")

        async def get_updates(self, **kwargs):
            self.pages += 1
            if self.pages == 1:
                assert kwargs["offset"] is None
                return old_updates
            assert kwargs["offset"] == 107
            return []

    class _DiscoverBot:
        session = _Session("discover")

        async def get_updates(self, **kwargs):
            assert kwargs["offset"] == 107
            return [ignored_group, new_start]

    bots = iter([_PrepareBot(), _DiscoverBot()])
    monkeypatch.setattr(TelegramSetupClient, "_bot", staticmethod(lambda _token: next(bots)))
    client = TelegramSetupClient()

    identity, offset = client.prepare("123456:test-token")
    operators = client.discover("123456:test-token", offset=offset, timeout_seconds=5.0)

    assert identity == TelegramBotIdentity(99, "jarn_bot", "Jarn Bot")
    assert offset == 107
    assert operators == [TelegramOperator(24680, "owner", "Jarn Owner")]
    assert closed == ["prepare", "discover"]


def test_gateway_setup_cancel_before_save_makes_no_changes(tmp_path, monkeypatch, capsys):
    path = _config(tmp_path, monkeypatch)
    original = path.read_bytes()
    token = "123456:never-stored-token"
    answers = iter(["", "no"])
    monkeypatch.setattr(
        "jarn.telegram.setup.activate_pending_credential",
        lambda *_args, **_kwargs: pytest.fail("credential must remain in memory"),
    )

    result = run_gateway_setup(
        client=_Client(),
        service_manager=_NoService(),
        input_fn=lambda _prompt: next(answers),
        secret_reader=lambda _prompt: token,
    )

    assert result == 130
    assert path.read_bytes() == original
    assert not list(path.parent.glob("config.yaml.bak.*"))
    captured = capsys.readouterr()
    assert "JARN-CLI-002" in captured.err
    assert token not in captured.out + captured.err

    # A malformed base config is rejected read-only before the bot token or
    # Telegram network are touched.
    malformed = b"config_version: [\n"
    path.write_bytes(malformed)

    class _MustNotConnect(_Client):
        def prepare(self, _token: str):
            pytest.fail("invalid base config must fail before Telegram")

    assert (
        run_gateway_setup(
            client=_MustNotConnect(),
            service_manager=_NoService(),
            secret_reader=lambda _prompt: pytest.fail("token must not be requested"),
        )
        == 2
    )
    assert path.read_bytes() == malformed
    assert "JARN-GATEWAY-002" in capsys.readouterr().err


def test_gateway_setup_rolls_back_fresh_credential_when_config_commit_fails(
    tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path, monkeypatch)
    original = path.read_bytes()
    token = "123456:rollback-token"
    activated = _fake_credential()
    rolled_back: list[ActivatedCredential] = []
    monkeypatch.setattr(
        "jarn.telegram.setup.activate_pending_credential",
        lambda *_args, **_kwargs: activated,
    )
    monkeypatch.setattr(
        "jarn.telegram.setup.commit_staged_config",
        lambda _staged: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr("jarn.telegram.setup.rollback_activated_credential", rolled_back.append)

    result = run_gateway_setup(
        client=_Client(),
        service_manager=_NoService(),
        allowed_users=[24680],
        assume_yes=True,
        no_service=True,
        secret_reader=lambda _prompt: token,
    )

    assert result == 1
    assert rolled_back == [activated]
    assert path.read_bytes() == original
    captured = capsys.readouterr()
    assert "JARN-GATEWAY-002" in captured.err
    assert token not in captured.out + captured.err


def test_gateway_setup_ambiguous_start_requires_explicit_choice(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    token = "123456:multi-user-token"
    client = _Client(
        [
            TelegramOperator(10, "one", "One"),
            TelegramOperator(20, "two", "Two"),
        ]
    )
    activated = _fake_credential()
    answers = iter(["", "2", "yes"])
    monkeypatch.setattr("jarn.telegram.setup.activate_pending_credential", lambda *_args: activated)
    monkeypatch.setattr("jarn.telegram.setup.resolve", lambda _reference: token)

    assert (
        run_gateway_setup(
            client=client,
            service_manager=_NoService(),
            no_service=True,
            input_fn=lambda _prompt: next(answers),
            secret_reader=lambda _prompt: token,
        )
        == 0
    )
    raw = yaml.safe_load((tmp_path / "home/config.yaml").read_text(encoding="utf-8"))
    assert raw["gateway"]["telegram"]["allowed_user_ids"] == [20]


def test_service_unit_contains_no_token_and_uses_owner_service(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / ".jarn"))
    manager = GatewayServiceManager(
        home=tmp_path,
        executable=tmp_path / "bin/jarn",
        platform_name="linux",
    )

    unit = manager.unit_text()

    assert "ExecStart=" in unit
    assert " gateway\n" in unit
    assert "RestartPreventExitStatus=75 76 77" in unit
    assert "UMask=0077" in unit
    assert "BOT_TOKEN" not in unit
    assert "token" not in unit.lower()
    assert GatewayServiceManager._quote("JARN_HOME=" + str(tmp_path / ".jarn")) in unit
    working = (
        str(tmp_path)
        .replace("%", "%%")
        .replace("\\", "\\\\")
        .replace(" ", "\\x20")
    )
    assert f"WorkingDirectory={working}\n" in unit
    assert f'WorkingDirectory="{tmp_path}"' not in unit


def test_service_unit_escapes_working_directory_without_value_quotes(
    tmp_path, monkeypatch
):
    home = tmp_path / "owner home%folder"
    monkeypatch.setenv("JARN_HOME", str(home / ".jarn"))
    manager = GatewayServiceManager(
        home=home,
        executable=home / "bin/jarn",
        platform_name="linux",
    )

    unit = manager.unit_text()

    expected = (
        str(home)
        .replace("%", "%%")
        .replace("\\", "\\\\")
        .replace(" ", "\\x20")
    )
    assert "WorkingDirectory=" + expected in unit
    assert "WorkingDirectory=\"" not in unit
    assert GatewayServiceManager._working_directory("/home/owner home%folder") == (
        "/home/owner\\x20home%%folder"
    )
    with pytest.raises(ValueError, match="must be absolute"):
        GatewayServiceManager._working_directory("relative")


def test_service_start_failure_includes_bounded_systemctl_diagnostic(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("jarn.telegram.setup.shutil.which", lambda name: f"/bin/{name}")

    def runner(argv, **_kwargs):
        if "restart" in argv:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr='WorkingDirectory= path is not absolute: "/home/operator"\n',
            )
        if "is-active" in argv or "is-enabled" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if argv[0] == "loginctl":
            return SimpleNamespace(returncode=0, stdout="yes\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    manager = GatewayServiceManager(
        home=tmp_path,
        executable=tmp_path / "bin/jarn",
        runner=runner,
        platform_name="linux",
    )

    with pytest.raises(RuntimeError, match="WorkingDirectory= path is not absolute"):
        manager.install_and_start()

    assert not manager.unit_path.exists()


def test_gateway_status_is_secret_free(tmp_path, monkeypatch, capsys):
    path = _config(tmp_path, monkeypatch)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["gateway"] = {
        "enabled": True,
        "telegram": {
            "token": "keychain:jarn/telegram.setup-test",
            "allowed_user_ids": [24680],
        },
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert run_gateway_status(service_manager=_NoService()) == 0
    output = capsys.readouterr().out
    assert "Bot token reference: configured" in output
    assert "Allowed operators: 1" in output
    assert "telegram.setup-test" not in output


def test_telegram_api_failure_never_renders_token(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    token = "123456:must-never-leak"

    class _FailingClient(_Client):
        def prepare(self, token_value: str):
            from jarn.errors import ErrorCode, error_detail

            raise TelegramSetupFailure(
                error_detail(
                    ErrorCode.GATEWAY_CREDENTIAL_INVALID,
                    "Telegram rejected the bot token.",
                    cause=f"unauthorized request containing {token_value}",
                    component="Telegram credential",
                    retryable=False,
                    action="Get a new token from @BotFather.",
                    known_secrets={token_value},
                ),
                3,
            )

    result = run_gateway_setup(
        client=_FailingClient(),
        service_manager=_NoService(),
        secret_reader=lambda _prompt: token,
    )

    assert result == 3
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert "[REDACTED]" in captured.err


def test_gateway_parser_exposes_standard_setup_and_service_controls():
    from jarn.cli import build_parser

    parser = build_parser()
    setup = parser.parse_args(
        [
            "gateway",
            "setup",
            "--token-stdin",
            "--allowed-user",
            "24680",
            "--no-service",
            "--yes",
        ]
    )
    assert setup.gateway_action == "setup"
    assert setup.allowed_user == [24680]
    assert setup.token_stdin is True
    assert setup.no_service is True
    assert setup.yes is True
    assert parser.parse_args(["gateway", "status"]).gateway_action == "status"
    assert (
        parser.parse_args(["gateway", "install-service"]).gateway_action
        == "install-service"
    )


def test_service_failure_keeps_verified_gateway_config_and_credential(
    tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path, monkeypatch)
    token = "123456:service-failure-token"
    activated = _fake_credential()
    rolled_back: list[ActivatedCredential] = []

    class _FailingService:
        unit_path = tmp_path / "jarn-telegram.service"

        def status(self):
            return ServiceStatus(True, False, False, False, True)

        def install_and_start(self):
            raise RuntimeError("systemd rejected WorkingDirectory")

    monkeypatch.setattr(
        "jarn.telegram.setup.activate_pending_credential",
        lambda *_args, **_kwargs: activated,
    )
    monkeypatch.setattr("jarn.telegram.setup.resolve", lambda _reference: token)
    monkeypatch.setattr("jarn.telegram.setup.rollback_activated_credential", rolled_back.append)

    result = run_gateway_setup(
        client=_Client(),
        service_manager=_FailingService(),
        allowed_users=[24680],
        assume_yes=True,
        secret_reader=lambda _prompt: token,
    )

    assert result == 1
    assert rolled_back == []
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["gateway"]["enabled"] is True
    assert raw["gateway"]["telegram"]["token"] == activated.reference
    assert raw["gateway"]["telegram"]["allowed_user_ids"] == [24680]
    captured = capsys.readouterr()
    assert "JARN-GATEWAY-005" in captured.err
    assert "configuration was kept" in captured.err
    assert "jarn gateway install-service" in captured.err
    assert token not in captured.out + captured.err


def test_install_service_reuses_existing_config_without_token_in_unit(
    tmp_path, monkeypatch, capsys
):
    manager = SimpleNamespace(
        unit_path=tmp_path / "jarn-telegram.service",
        install_and_start=lambda: ServiceStatus(True, True, True, True, True),
    )
    monkeypatch.setattr(
        "jarn.telegram.cli.load_gateway_settings",
        lambda: SimpleNamespace(token="must-not-render"),
    )

    assert run_gateway_service_install(service_manager=manager) == 0
    output = capsys.readouterr().out
    assert "enabled and running" in output
    assert "must-not-render" not in output


def test_service_status_fails_closed_when_user_manager_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("jarn.telegram.setup.shutil.which", lambda name: f"/bin/{name}")

    def runner(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not running")

    manager = GatewayServiceManager(
        home=tmp_path,
        executable=tmp_path / "jarn",
        runner=runner,
        platform_name="linux",
    )
    assert manager.status() == ServiceStatus(
        False, False, False, False, None, "user manager unavailable"
    )


def test_user_service_install_is_atomic_token_free_and_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / ".jarn"))
    monkeypatch.setenv("USER", "operator")
    monkeypatch.setattr("jarn.telegram.setup.shutil.which", lambda name: f"/bin/{name}")
    calls: list[list[str]] = []
    started = False

    def runner(argv, **_kwargs):
        nonlocal started
        calls.append(list(argv))
        if argv[2] == "restart":
            started = True
        if "is-active" in argv or "is-enabled" in argv:
            return SimpleNamespace(returncode=0 if started else 1, stdout="", stderr="")
        if argv[0] == "loginctl":
            return SimpleNamespace(returncode=0, stdout="no\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    manager = GatewayServiceManager(
        home=tmp_path,
        executable=tmp_path / "bin/jarn",
        runner=runner,
        platform_name="linux",
    )

    status = manager.install_and_start()

    assert status.active is True
    assert status.enabled is True
    assert status.linger is False
    unit = manager.unit_path.read_text(encoding="utf-8")
    assert "ExecStart=" in unit
    assert "token" not in unit.lower()
    assert manager.unit_path.stat().st_mode & 0o777 == 0o600
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert [
        "systemctl",
        "--user",
        "enable",
        "jarn-telegram.service",
    ] in calls
    assert [
        "systemctl",
        "--user",
        "restart",
        "jarn-telegram.service",
    ] in calls
