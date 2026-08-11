"""Crash-safe, non-secret progress for resumable first-run setup."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarn.config import paths
from jarn.util.atomic import atomic_write_text, file_lock

SETUP_STATE_SCHEMA_VERSION = 1
_STAGE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_ANSWER_KEYS = frozenset(
    {
        "provider",
        "_provider_group",
        "storage",
        "key_ref",
        "base_url",
        "model",
        "theme",
        "reasoning_effort",
        "routing_subagent",
        "routing_summarizer",
        "routing_fallback",
        "budget_per_session_usd",
        "budget_warn_at_pct",
        "budget_hard_stop",
        "permission_mode",
        "_credential_pending",
    }
)


def default_setup_state_path() -> Path:
    return paths.global_home() / "setup-state.json"


@dataclass(frozen=True, slots=True)
class SetupState:
    stage: str
    answers: dict[str, str]
    updated_at: str
    schema_version: int = SETUP_STATE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "answers": dict(self.answers),
            "updated_at": self.updated_at,
        }


def _safe_answers(answers: dict[str, Any]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in answers.items():
        if key not in _SAFE_ANSWER_KEYS or not isinstance(value, str):
            continue
        if key == "key_ref" and not value.startswith(("${", "keychain:", "file:")):
            # A raw pasted key must only ever flow into the secret store. Refuse
            # to turn setup recovery into a second credential database.
            continue
        safe[key] = value
    return safe


def save_setup_state(
    stage: str,
    answers: dict[str, Any],
    *,
    path: Path | None = None,
    now: str | None = None,
) -> SetupState:
    """Persist one resumable boundary without writing any credential value."""

    if _STAGE_RE.fullmatch(stage) is None:
        raise ValueError(f"invalid setup stage: {stage!r}")
    target = path or default_setup_state_path()
    state = SetupState(
        stage=stage,
        answers=_safe_answers(answers),
        updated_at=now or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    payload = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with file_lock(target):
        atomic_write_text(target, payload + "\n", mode=0o600)
    return state


def load_setup_state(*, path: Path | None = None) -> SetupState | None:
    """Load a valid progress record; malformed/untrusted records are ignored."""

    target = path or default_setup_state_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != SETUP_STATE_SCHEMA_VERSION:
        return None
    stage = raw.get("stage")
    answers = raw.get("answers")
    updated_at = raw.get("updated_at")
    if (
        not isinstance(stage, str)
        or _STAGE_RE.fullmatch(stage) is None
        or not isinstance(answers, dict)
        or not isinstance(updated_at, str)
    ):
        return None
    safe = _safe_answers(answers)
    if len(safe) != len(answers):
        return None
    return SetupState(stage=stage, answers=safe, updated_at=updated_at)


def clear_setup_state(*, path: Path | None = None) -> None:
    """Remove only J.A.R.N.'s setup-progress record after verified completion."""

    target = path or default_setup_state_path()
    with file_lock(target):
        try:
            target.unlink()
        except FileNotFoundError:
            return


__all__ = [
    "SETUP_STATE_SCHEMA_VERSION",
    "SetupState",
    "clear_setup_state",
    "default_setup_state_path",
    "load_setup_state",
    "save_setup_state",
]
