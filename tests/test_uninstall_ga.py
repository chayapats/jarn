"""GA data-preserving uninstall contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _record(active: Path, state: Path, *, candidate: Path | None = None):
    from jarn.install_state import InstallRecord

    return InstallRecord(
        schema_version=1,
        version="2.0.0",
        method="python",
        channel="stable",
        active_path=active,
        candidate_path=candidate,
        state_dir=state,
        dependency={
            "uv_path": str(active.parent / "uv"),
            "uv_owned_by_jarn": True,
        },
    )


def test_interactive_defaults_remove_executable_but_preserve_user_data(
    tmp_path, monkeypatch
) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("theme: dark\n", encoding="utf-8")
    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / "thread.jsonl").write_text("{}\n", encoding="utf-8")
    active = tmp_path / "bin" / "jarn"
    active.parent.mkdir()
    active.write_text("binary", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    manifest = state / "install.json"
    manifest.write_text("{}", encoding="utf-8")
    record = _record(active, state)

    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: record)
    answers = iter(["", "n", "n", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_uninstall() == 0
    assert not active.exists()
    assert not manifest.exists()
    assert config.exists()
    assert sessions.exists()


@pytest.mark.parametrize("exception_type", [EOFError, KeyboardInterrupt])
@pytest.mark.parametrize("prompt_index", range(6))
def test_interactive_prompt_interrupt_at_every_category_is_atomic_cancel(
    tmp_path, monkeypatch, capsys, exception_type, prompt_index
) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    config = home / "config.yaml"
    transcript = home / "sessions" / "thread.jsonl"
    config_bytes = b"theme: dark\n"
    transcript_bytes = b"{}\n"
    config.write_bytes(config_bytes)
    transcript.write_bytes(transcript_bytes)
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)
    removals: list[Path] = []
    keychain_deletes: list[str] = []
    monkeypatch.setattr("jarn.uninstall._remove_path", removals.append)
    monkeypatch.setattr(
        "jarn.uninstall.delete_keychain_secret",
        lambda _service, account, *, timeout: keychain_deletes.append(account),
    )
    prompt_count = 0

    def interrupt_one(_prompt: str) -> str:
        nonlocal prompt_count
        current = prompt_count
        prompt_count += 1
        if current == prompt_index:
            raise exception_type()
        return "y"

    monkeypatch.setattr("builtins.input", interrupt_one)

    assert run_uninstall() == 130
    error = capsys.readouterr().err
    assert "JARN-CLI-002" in error
    assert "Nothing was removed" in error
    assert all(field in error for field in ("Cause:", "Component:", "Next:", "Log:"))
    assert removals == []
    assert keychain_deletes == []
    assert config.read_bytes() == config_bytes
    assert transcript.read_bytes() == transcript_bytes


@pytest.mark.parametrize("exception_type", [EOFError, KeyboardInterrupt])
def test_explicit_category_prompt_interrupt_discards_prior_yes_without_mutation(
    tmp_path, monkeypatch, capsys, exception_type
) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    config = home / "config.yaml"
    transcript = home / "sessions" / "thread.jsonl"
    config.write_text("keep config\n", encoding="utf-8")
    transcript.write_text("keep session\n", encoding="utf-8")
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)
    removals: list[Path] = []
    monkeypatch.setattr("jarn.uninstall._remove_path", removals.append)
    answers = iter(["y"])

    def interrupt_second(_prompt: str) -> str:
        try:
            return next(answers)
        except StopIteration:
            raise exception_type() from None

    monkeypatch.setattr("builtins.input", interrupt_second)

    assert run_uninstall(categories={"config", "sessions"}) == 130
    error = capsys.readouterr().err
    assert "JARN-CLI-002" in error
    assert "sessions" in error
    assert removals == []
    assert config.read_text(encoding="utf-8") == "keep config\n"
    assert transcript.read_text(encoding="utf-8") == "keep session\n"


def test_declining_every_category_is_explicit_cancel_with_zero_mutation(
    tmp_path, monkeypatch, capsys
) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    original = b"preserve\n"
    config.write_bytes(original)
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)
    removals: list[Path] = []
    monkeypatch.setattr("jarn.uninstall._remove_path", removals.append)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert run_uninstall() == 130
    error = capsys.readouterr().err
    assert "JARN-CLI-002" in error
    assert "No uninstall category was confirmed" in error
    assert removals == []
    assert config.read_bytes() == original


@pytest.mark.parametrize("exception_type", [EOFError, KeyboardInterrupt])
def test_cli_propagates_prompt_cancel_exit_and_preserves_bytes(
    tmp_path, monkeypatch, capsys, exception_type
) -> None:
    from jarn.cli import main
    from jarn.config import paths

    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    original = b"preserve through cli\n"
    config.write_bytes(original)
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)

    def interrupt(_prompt: str) -> str:
        raise exception_type()

    monkeypatch.setattr("builtins.input", interrupt)

    assert main(["uninstall"]) == 130
    error = capsys.readouterr().err
    assert "JARN-CLI-002" in error
    assert all(field in error for field in ("Cause:", "Component:", "Next:", "Log:"))
    assert config.read_bytes() == original


def test_explicit_credentials_category_does_not_remove_config(tmp_path, monkeypatch) -> None:
    from jarn.config import paths
    from jarn.config.defaults import ALL_PROVIDERS
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    (home / "secrets" / "jarn").mkdir(parents=True)
    (home / "secrets" / "jarn" / "openai").write_text("secret", encoding="utf-8")
    config = home / "config.yaml"
    config.write_text("provider: openai\n", encoding="utf-8")
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)
    deleted: list[tuple[str, str, float]] = []
    monkeypatch.setattr(
        "jarn.uninstall.delete_keychain_secret",
        lambda service, account, *, timeout: deleted.append((service, account, timeout)),
    )

    assert run_uninstall(yes=True, categories={"credentials"}) == 0
    assert config.exists()
    assert not (home / "secrets").exists()
    assert len(deleted) == len(ALL_PROVIDERS)
    assert all(service == "jarn" and timeout == 2.0 for service, _account, timeout in deleted)


def test_noninteractive_yes_without_categories_preserves_all_user_data(
    tmp_path, monkeypatch
) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    config = home / "config.yaml"
    session = home / "sessions" / "thread.jsonl"
    config.write_text("theme: dark\n", encoding="utf-8")
    session.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)
    assert run_uninstall(yes=True) == 0
    assert config.exists()
    assert session.exists()


def test_missing_keychain_entries_are_benign_typed_failures(tmp_path, monkeypatch) -> None:
    from jarn.config import paths
    from jarn.config.secrets import KeyringOperationError
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)
    monkeypatch.setattr(
        "jarn.uninstall.delete_keychain_secret",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyringOperationError("delete", "PasswordDeleteError")
        ),
    )

    assert run_uninstall(yes=True, categories={"credentials"}) == 0


def test_blocked_keychain_stops_after_one_timeout_and_redacts_error(
    tmp_path, monkeypatch, capsys
) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)
    calls: list[str] = []
    leaked = "sk-proj-UNINSTALLSECRET1234567890ABCD"

    def _blocked(_service, account, *, timeout):
        calls.append(account)
        raise TimeoutError(f"backend blocked with {leaked}")

    monkeypatch.setattr("jarn.uninstall.delete_keychain_secret", _blocked)

    assert run_uninstall(yes=True, categories={"credentials"}) == 1
    assert len(calls) == 1
    error = capsys.readouterr().err
    assert leaked not in error
    assert "sk-…" in error
    assert "JARN-UNINSTALL-002" in error
    assert all(field in error for field in ("Cause:", "Component:", "Next:", "Log:"))


def test_shared_uv_is_never_in_exclusively_owned_dependency_plan(tmp_path) -> None:
    from jarn.uninstall import build_uninstall_plan

    home = tmp_path / "home"
    state = tmp_path / "state"
    tool = state / "versions" / "python-2" / "bin" / "jarn"
    tool.parent.mkdir(parents=True)
    tool.write_text("tool", encoding="utf-8")
    active = tmp_path / "bin" / "jarn"
    uv = active.parent / "uv"
    active.parent.mkdir()
    active.write_text("link", encoding="utf-8")
    uv.write_text("shared dependency", encoding="utf-8")
    plan = build_uninstall_plan(home=home, record=_record(active, state, candidate=tool))
    dependencies = next(item for item in plan if item.category == "dependencies")
    assert state / "versions" / "python-2" in dependencies.paths
    assert uv not in dependencies.paths


def test_material_remove_failure_returns_nonzero(tmp_path, monkeypatch, capsys) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)

    leaked = "sk-proj-REMOVESECRET1234567890ABCD"

    def fail(_path):
        raise PermissionError(f"read-only backend echoed {leaked}")

    monkeypatch.setattr("jarn.uninstall._remove_path", fail)
    assert run_uninstall(yes=True, categories={"config"}) == 1
    error = capsys.readouterr().err
    assert "uninstall incomplete" in error.lower()
    assert "JARN-UNINSTALL-002" in error
    assert all(field in error for field in ("Cause:", "Component:", "Next:", "Log:"))
    assert leaked not in error
    assert "sk-…" in error
    assert config.exists()


def test_unknown_uninstall_category_has_full_usage_anatomy(tmp_path, monkeypatch, capsys):
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)

    leaked = "sk-proj-UNKNOWNCATEGORY1234567890ABCD"
    assert run_uninstall(yes=True, categories={leaked}) == 2
    error = capsys.readouterr().err
    assert "JARN-UNINSTALL-001" in error
    assert all(field in error for field in ("Cause:", "Component:", "Next:", "Log:"))
    assert leaked not in error
    assert "sk-…" in error


def test_unsafe_receipt_failure_uses_redacted_error_detail(tmp_path, monkeypatch, capsys) -> None:
    from jarn.config import paths
    from jarn.install_state import InstallStateError
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    sentinel = home / "config.yaml"
    sentinel.write_bytes(b"preserve\n")
    leaked = "sk-proj-RECEIPTSECRET1234567890ABCD"
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr(
        "jarn.uninstall._load_record_best_effort",
        lambda: (_ for _ in ()).throw(InstallStateError(f"unsafe receipt {leaked}")),
    )

    assert run_uninstall(yes=True) == 2
    error = capsys.readouterr().err
    assert "JARN-UNINSTALL-003" in error
    assert all(field in error for field in ("Cause:", "Component:", "Next:", "Log:"))
    assert leaked not in error
    assert "sk-…" in error
    assert sentinel.read_bytes() == b"preserve\n"


def test_inventory_failure_is_redacted_and_cannot_mutate_data(
    tmp_path, monkeypatch, capsys
) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    sentinel = home / "config.yaml"
    sentinel.write_bytes(b"preserve inventory failure\n")
    leaked = "sk-proj-INVENTORYSECRET1234567890ABCD"
    removals: list[Path] = []
    monkeypatch.setattr(paths, "global_home", lambda: home)
    monkeypatch.setattr("jarn.uninstall._load_record_best_effort", lambda: None)
    monkeypatch.setattr(
        "jarn.uninstall.build_uninstall_plan",
        lambda **_kwargs: (_ for _ in ()).throw(
            PermissionError(f"inventory backend echoed {leaked}")
        ),
    )
    monkeypatch.setattr("jarn.uninstall._remove_path", removals.append)

    assert run_uninstall(yes=True, categories={"config"}) == 1
    error = capsys.readouterr().err
    assert "JARN-UNINSTALL-002" in error
    assert "Nothing was removed" in error
    assert all(field in error for field in ("Cause:", "Component:", "Next:", "Log:"))
    assert leaked not in error
    assert "sk-…" in error
    assert removals == []
    assert sentinel.read_bytes() == b"preserve inventory failure\n"


def test_tampered_receipt_cannot_delete_unrelated_path(tmp_path, monkeypatch, capsys) -> None:
    from jarn.config import paths
    from jarn.uninstall import run_uninstall

    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    unrelated = tmp_path / "documents" / "jarn"
    unrelated.parent.mkdir()
    unrelated.write_text("valuable", encoding="utf-8")
    receipt = {
        "schema_version": 1,
        "version": "2.0.0",
        "method": "binary",
        "channel": "stable",
        "active_path": str(unrelated),
        "state_dir": str(state),
    }
    (state / "install.json").write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setenv("JARN_STATE_DIR", str(state))
    monkeypatch.setattr(paths, "global_home", lambda: home)

    assert run_uninstall(yes=True) == 2
    assert unrelated.read_text(encoding="utf-8") == "valuable"
    error = capsys.readouterr().err
    assert "JARN-UNINSTALL-003" in error
    assert "Next:" in error and "Log:" in error
