from __future__ import annotations

import json
import os
from pathlib import Path

from jarn.onboarding.state import clear_setup_state, load_setup_state, save_setup_state


def test_setup_state_round_trips_unicode_references_privately(tmp_path: Path):
    target = tmp_path / "setup-state.json"

    save_setup_state(
        "model",
        {
            "provider": "anthropic",
            "model": "anthropic/โมเดล",
            "key_ref": "keychain:jarn/anthropic",
        },
        path=target,
        now="2026-08-09T00:00:00Z",
    )

    state = load_setup_state(path=target)
    assert state is not None
    assert state.stage == "model"
    assert state.answers["model"].endswith("โมเดล")
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_setup_state_never_persists_raw_key_or_unknown_answers(tmp_path: Path):
    target = tmp_path / "setup-state.json"
    secret = "sk-ant-super-secret"

    save_setup_state(
        "key",
        {"provider": "anthropic", "key_ref": secret, "raw_key": secret},
        path=target,
    )

    payload = target.read_text(encoding="utf-8")
    assert secret not in payload
    assert json.loads(payload)["answers"] == {"provider": "anthropic"}


def test_malformed_or_future_setup_state_is_not_resumed(tmp_path: Path):
    target = tmp_path / "setup-state.json"
    target.write_text('{"schema_version":99}', encoding="utf-8")
    assert load_setup_state(path=target) is None
    target.write_text("not-json", encoding="utf-8")
    assert load_setup_state(path=target) is None


def test_clear_setup_state_is_scoped_and_idempotent(tmp_path: Path):
    target = tmp_path / "setup-state.json"
    sibling = tmp_path / "config.yaml"
    target.write_text("{}", encoding="utf-8")
    sibling.write_text("keep", encoding="utf-8")

    clear_setup_state(path=target)
    clear_setup_state(path=target)

    assert not target.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
