"""Focused host-inventory and doctor service contract tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarn.doctor.inventory as inventory
import jarn.doctor.service as service
from jarn.catalog import ModelCatalogService
from jarn.doctor.collect import collect_doctor

FAKE_CODEX = Path(__file__).with_name("codex_fake_app_server.py")


@pytest.fixture(autouse=True)
def _isolate_catalog_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Doctor tests must never read or populate the operator's real model cache."""

    from jarn.config import paths

    monkeypatch.setattr(paths, "cachedir", lambda: tmp_path / "cache")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode")
def test_command_inventory_reports_every_path_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        executable = directory / "jarn"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(first), str(second))))

    candidates, _checks = inventory._discover_command_inventory(
        "jarn", home=tmp_path / "empty-home"
    )
    assert [item["path"] for item in candidates[:2]] == [
        str(first / "jarn"),
        str(second / "jarn"),
    ]
    assert all(item["on_path"] for item in candidates[:2])


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixtures")
def test_command_inventory_discovers_off_path_package_manager_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    manager_bin = tmp_path / "manager-bin"
    manager_bin.mkdir()
    home = tmp_path / "home"
    pip_user = tmp_path / "pip-user"
    pipx_bin = tmp_path / "pipx-bin"
    uv_bin = tmp_path / "uv-bin"
    npm_prefix = tmp_path / "npm-prefix"
    npm_root = tmp_path / "npm-root"
    brew_prefix = tmp_path / "brew-prefix"
    nvm_bin = home / ".nvm" / "versions" / "node" / "v22" / "bin"

    def executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    for directory in (
        pip_user / "bin",
        pipx_bin,
        uv_bin,
        npm_prefix / "bin",
        brew_prefix / "bin",
        nvm_bin,
    ):
        executable(directory / "jarn")
    package = npm_root / "jarn-cli" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"name":"jarn-cli"}\n', encoding="utf-8")

    executable(manager_bin / "python3", f"#!/bin/sh\nprintf '%s\\n' '{pip_user}'\n")
    executable(manager_bin / "pipx", f"#!/bin/sh\nprintf '%s\\n' '{pipx_bin}'\n")
    executable(manager_bin / "uv", f"#!/bin/sh\nprintf '%s\\n' '{uv_bin}'\n")
    executable(
        manager_bin / "npm",
        "#!/bin/sh\n"
        f"if [ \"$1\" = prefix ]; then printf '%s\\n' '{npm_prefix}'; "
        f"else printf '%s\\n' '{npm_root}'; fi\n",
    )
    executable(manager_bin / "brew", f"#!/bin/sh\nprintf '%s\\n' '{brew_prefix}'\n")

    monkeypatch.setenv("PATH", str(manager_bin))
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)
    monkeypatch.setattr(inventory.site, "getuserbase", lambda: str(pip_user))

    # This scenario verifies discovery breadth, not timeout behavior. Leave
    # enough startup budget for loaded CI/macOS hosts; the dedicated timeout
    # test below uses a deliberately wedged manager and a 250 ms bound.
    candidates, checks = inventory._discover_command_inventory("jarn", home=home, timeout=2.0)
    by_path = {item["path"]: item for item in candidates}

    expected = {
        str(pip_user / "bin" / "jarn"): "pip-user",
        str(pipx_bin / "jarn"): "pipx",
        str(uv_bin / "jarn"): "uv-tool",
        str(npm_prefix / "bin" / "jarn"): "npm",
        str(brew_prefix / "bin" / "jarn"): "homebrew",
        str(nvm_bin / "jarn"): "npm-nvm",
    }
    for path, source in expected.items():
        assert path in by_path, {
            "missing": path,
            "source": source,
            "check": next((item for item in checks if item["manager"] == source), None),
        }
        assert source in by_path[path]["sources"]
        assert by_path[path]["on_path"] is False
        assert by_path[path]["usable"] is True
    assert {check["manager"] for check in checks} >= {
        "pip-user",
        "pipx",
        "uv-tool",
        "npm-prefix",
        "npm-root",
        "homebrew",
    }
    npm_check = next(check for check in checks if check["manager"] == "npm-root")
    assert npm_check["jarn_package_present"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixtures")
def test_package_manager_inventory_timeout_is_bounded_and_nonfatal(monkeypatch, tmp_path):
    manager_bin = tmp_path / "manager-bin"
    manager_bin.mkdir()
    uv = manager_bin / "uv"
    late_path = tmp_path / "late-write"
    uv.write_text(
        f"#!/bin/sh\n/bin/sleep 5\nprintf '%s' mutated > \"{late_path}\"\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(manager_bin))
    monkeypatch.setattr(inventory.site, "getuserbase", lambda: str(tmp_path / "missing"))
    real_popen = inventory.subprocess.Popen
    worker_pids: list[int] = []

    def _tracked_popen(args, **kwargs):
        process = real_popen(args, **kwargs)
        if args[0] == str(uv):
            worker_pids.append(process.pid)
        return process

    monkeypatch.setattr(inventory.subprocess, "Popen", _tracked_popen)

    candidates, checks = inventory._discover_command_inventory(
        "jarn", home=tmp_path / "home", timeout=0.25
    )

    assert all("uv-tool" not in item["sources"] for item in candidates)
    uv_check = next(check for check in checks if check["manager"] == "uv-tool")
    assert uv_check["timed_out"] is True
    assert uv_check["timeout_seconds"] == 0.25
    assert uv_check["action"]
    assert len(worker_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pids[0], 0)
    assert not late_path.exists()


def test_command_inventory_rejects_path_injection_before_subprocess():
    with pytest.raises(ValueError, match="invalid command name"):
        inventory._discover_command_inventory("../jarn")


def test_human_doctor_renders_off_path_installation_with_source():
    from jarn.doctor.render import append_inventory_lines

    lines: list[str] = []
    append_inventory_lines(
        lines,
        {
            "jarn": {
                "version": "0.11.0",
                "active_executable": "/usr/bin/jarn",
                "install": {"method": "system-package"},
                "candidate_inventory": [
                    {
                        "path": "/home/user/.local/pipx-bin/jarn",
                        "sources": ["pipx"],
                        "active": False,
                        "on_path": False,
                    }
                ],
            }
        },
    )

    rendered = "\n".join(lines)
    assert "other installation (off PATH)" in rendered
    assert "pipx" in rendered


def test_install_metadata_json_is_parsed_not_reported_as_brace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from jarn.config import paths

    home = tmp_path / ".jarn"
    home.mkdir()
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "missing-state"))
    (home / "install.json").write_text(
        '{"method":"portable-python","version":"1.2.3","channel":"stable",'
        '"activation":{"status":"verified"},"setup_status":"complete"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "global_home", lambda: home)

    result = inventory._install_method(tmp_path / "bin" / "jarn")

    assert result == {
        "method": "portable-python",
        "metadata_present": True,
        "metadata_source": "legacy",
        "version": "1.2.3",
        "channel": "stable",
        "activation_status": "verified",
        "setup_status": "complete",
        "canonical_record_error": None,
    }


def test_inventory_prefers_shared_canonical_install_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    state = tmp_path / "state"
    state.mkdir()
    active = tmp_path / "bin" / "jarn"
    active.parent.mkdir()
    active.write_text("executable", encoding="utf-8")
    (state / "install.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "2.0.0",
                "method": "standalone",
                "channel": "stable",
                "active_path": str(active),
                "state_dir": str(state),
                "activation": {"status": "verified"},
                "setup_status": "complete",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARN_STATE_DIR", str(state))

    result = inventory._install_method(active)

    assert result["method"] == "standalone"
    assert result["metadata_source"] == "canonical-install-record"
    assert result["active_matches_record"] is True
    assert result["activation_status"] == "verified"


def test_inventory_surfaces_malformed_canonical_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from jarn.config import paths

    state = tmp_path / "state"
    state.mkdir()
    (state / "install.json").write_text("{", encoding="utf-8")
    monkeypatch.setenv("JARN_STATE_DIR", str(state))
    monkeypatch.setattr(paths, "global_home", lambda: tmp_path / "legacy-home")

    result = inventory._install_method(None)

    assert result["metadata_present"] is False
    assert "JARN-UPDATE-005" in result["canonical_record_error"]


def test_host_inventory_contains_ga_resolution_sandbox_secret_and_update_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from jarn.config import paths

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(paths, "cachedir", lambda: tmp_path / "cache")

    result = inventory.collect_host_inventory(project_root=tmp_path, project_trusted=True)

    assert "candidate_inventory" in result["jarn"]
    assert "discovery_checks" in result["jarn"]
    assert result["shell"]["resolution"]["checked"] is True
    assert "hash_cache_action" in result["shell"]["resolution"]
    assert "parent_shell_limitation" in result["shell"]
    assert {"native_available", "docker_available"} <= result["sandbox"].keys()
    assert result["secrets"]["keyring"]["credentials_read"] is False
    assert {"cache_path", "cache_present"} <= result["updates"].keys()


def test_keyring_inventory_uses_bounded_metadata_only_probe(monkeypatch):
    calls: list[float] = []

    def _metadata(*, timeout: float):
        calls.append(timeout)
        return {
            "available": True,
            "backend": "keyring.backends.test.Keyring",
            "priority": 1,
            "credentials_read": False,
        }

    monkeypatch.setattr(inventory, "keyring_backend_metadata", _metadata)

    result = inventory._keyring_inventory(timeout=0.25)

    assert calls == [0.25]
    assert result["available"] is True
    assert result["credentials_read"] is False


def test_keyring_inventory_timeout_is_nonfatal_and_actionable(monkeypatch):
    monkeypatch.setattr(
        inventory,
        "keyring_backend_metadata",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("blocked backend")),
    )

    result = inventory._keyring_inventory(timeout=0.125)

    assert result["available"] is False
    assert result["credentials_read"] is False
    assert result["timed_out"] is True
    assert result["timeout_seconds"] == 0.125
    assert result["awaited"] == "OS keychain backend metadata"
    assert result["action"]


def test_network_probe_is_per_endpoint_bounded_and_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
):
    received: list[tuple[tuple[str, int], float]] = []

    def refuse(address: tuple[str, int], *, timeout: float):
        received.append((address, timeout))
        raise TimeoutError("bounded timeout")

    monkeypatch.setattr(inventory.socket, "create_connection", refuse)
    config = SimpleNamespace(
        providers={
            "one": SimpleNamespace(base_url="https://api.example.invalid/v1"),
            "duplicate": SimpleNamespace(base_url="https://api.example.invalid/v2"),
        }
    )

    result = inventory.collect_provider_reachability(config, timeout=0.125)

    assert result["checked"] is True
    assert received == [(("api.example.invalid", 443), 0.125)]
    assert result["checks"][0]["reachable"] is False
    assert result["checks"][0]["timed_out"] is True
    assert result["checks"][0]["timeout_seconds"] == 0.125
    assert result["checks"][0]["action"]


def test_doctor_service_defaults_to_offline_nonmutating_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    home = tmp_path / ".jarn"
    home.mkdir(mode=0o755)

    def fake_collect(diag: dict, **kwargs) -> int:
        assert kwargs["network"] is False
        diag.update({"ok": True, "configuration": {}, "secrets": {}})
        return 0

    monkeypatch.setattr(service, "collect_doctor", fake_collect)

    result = service.run_doctor_service(fix=True, global_home=home)

    assert result.ok is True
    assert result.repair_result is not None
    assert result.repair_result.dry_run is True
    assert result.repair_plan.has_changes is (os.name != "nt")
    if os.name != "nt":
        assert (home.stat().st_mode & 0o777) == 0o755


def test_doctor_turns_corrupt_config_into_structured_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    home = tmp_path / ".jarn"
    home.mkdir()
    (home / "config.yaml").write_text("providers: [\n", encoding="utf-8")
    monkeypatch.setenv("JARN_HOME", str(home))
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "missing-state"))
    monkeypatch.setenv("PATH", "")

    diag: dict = {}
    code = collect_doctor(
        diag,
        project_root=tmp_path,
        project_trusted=True,
        prompt_modules={"modules": []},
    )

    assert code == 1
    assert diag["ok"] is False
    assert diag["configuration"]["global"]["status"] == "corrupt"
    assert diag["config_error"]["code"] == "JARN-CONFIG-001"
    assert set(diag["config_error"]) >= {
        "summary",
        "cause",
        "component",
        "retryable",
        "action",
        "log_path",
    }
    assert "Traceback" not in json.dumps(diag)


def _codex_config(*, subagent: str = "codex_subscription/gpt-default"):
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig

    ref = "codex_subscription/gpt-default"
    return Config(
        default_profile="codex_subscription",
        default_model=ref,
        providers={
            "codex_subscription": ProviderConfig(
                type=ProviderType.CODEX_SUBSCRIPTION,
                extra={
                    "codex_command": [sys.executable, str(FAKE_CODEX)],
                    "reasoning_effort": "medium",
                },
            )
        },
        routing=RoutingConfig(
            main=ref,
            subagent=subagent,
            summarizer=ref,
        ),
    )


def test_doctor_uses_unified_auth_and_live_catalog(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_paginated")

    diag: dict = {}
    code = collect_doctor(
        diag,
        config=_codex_config(),
        project_root=tmp_path,
        project_trusted=True,
        prompt_modules={"modules": []},
        network=True,
    )

    assert code == 0
    assert diag["auth"]["codex"]["state"] == "authenticated_chatgpt"
    assert diag["catalog"]["source"] == "codex_live"
    assert diag["catalog"]["availability_verified"] is True
    assert diag["catalog"]["model_count"] == 2
    assert diag["selected_route"]["available"] is True
    assert diag["selected_route"]["routes_checked"] is True


def test_doctor_default_is_cache_only_and_does_not_claim_uncached_model_ready(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_paginated")

    seen_refresh: list[bool] = []
    original = ModelCatalogService.get_catalogs_for_routes

    def spy(self, *args, **kwargs):
        seen_refresh.append(kwargs["refresh_live"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ModelCatalogService, "get_catalogs_for_routes", spy)
    diag: dict = {}

    code = collect_doctor(
        diag,
        config=_codex_config(),
        project_root=tmp_path,
        project_trusted=True,
        prompt_modules={"modules": []},
    )

    assert seen_refresh == [False]
    assert code == 1
    assert diag["catalog"]["source"] == "static_fallback"
    assert diag["catalog"]["availability_verified"] is False
    assert diag["selected_route"]["available"] is False


def test_doctor_rejects_removed_background_route_before_turn(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_paginated")

    diag: dict = {}
    code = collect_doctor(
        diag,
        config=_codex_config(subagent="codex_subscription/retired-model"),
        project_root=tmp_path,
        project_trusted=True,
        prompt_modules={"modules": []},
        network=True,
    )

    assert code == 1
    assert diag["selected_route"]["available"] is False
    assert "subagent" in diag["selected_route"]["error"]
    assert "retired-model" in diag["selected_route"]["error"]


def test_doctor_checks_background_route_on_another_provider(monkeypatch, tmp_path: Path):
    from jarn.config.schema import ProviderConfig, ProviderType
    from jarn.providers import RemoteModelCatalog, RemoteModelRecord

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_paginated")
    config = _codex_config(subagent="ollama/retired-local-model")
    config.providers["ollama"] = ProviderConfig(
        type=ProviderType.OLLAMA,
        base_url="http://127.0.0.1:11434",
    )
    monkeypatch.setattr(
        "jarn.catalog.service.fetch_remote_model_catalog",
        lambda _provider, **_kwargs: RemoteModelCatalog(
            (RemoteModelRecord("available-local-model"),),
            "live local fixture",
            "local-scope",
        ),
    )

    diag: dict = {}
    code = collect_doctor(
        diag,
        config=config,
        project_root=tmp_path,
        project_trusted=True,
        prompt_modules={"modules": []},
        network=True,
    )

    assert code == 1
    assert set(diag["catalogs"]) == {"codex_subscription", "ollama"}
    assert diag["selected_route"]["available"] is False
    assert "subagent" in diag["selected_route"]["error"]
    assert "retired-local-model" in diag["selected_route"]["error"]


def test_doctor_render_boundaries_redact_resolved_opaque_credentials(monkeypatch):
    import jarn.config.secrets as secrets
    from jarn.doctor.render import _esc, doctor_to_json

    credential = "arbitrary-provider-key-7q2"
    secrets._clear_resolved_secrets_for_testing()
    try:
        monkeypatch.setenv("DOCTOR_OPAQUE_KEY", credential)
        assert secrets.resolve("${DOCTOR_OPAQUE_KEY}") == credential
        diag = {"ok": False, "provider_error": f"upstream echoed {credential}"}

        machine = doctor_to_json(diag)
        human_fragment = _esc(diag["provider_error"])
        assert credential not in machine
        assert credential not in human_fragment
        assert "[REDACTED]" in machine
    finally:
        secrets._clear_resolved_secrets_for_testing()
