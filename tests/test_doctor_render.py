"""S10 — human ``jarn doctor`` chrome via ``t()``; ``--json`` keys stay English."""

from __future__ import annotations

import json
import re

import pytest

from jarn.doctor.render import doctor_lines, doctor_to_json
from jarn.tui.i18n import t

_MARKUP = re.compile(r"\[/?[^\]]+\]")


def _plain(text: str) -> str:
    return _MARKUP.sub("", text)


def _body(diag: dict, locale: str) -> str:
    return _plain("\n".join(doctor_lines(diag, locale=locale)))


def _min_diag(**overrides: object) -> dict:
    diag: dict = {
        "ok": True,
        "global_config": "/tmp/config.yaml",
        "global_config_present": True,
        "project_root": "/tmp/proj",
        "default_profile": "openrouter",
        "main_model": "openrouter/claude",
        "main_model_builds": True,
        "permission_mode": "ask",
        "effective_mode": "ask",
        "web_tools": True,
        "sandbox": {"backend": "none", "available": False, "mode": "off"},
        "execution": {"backend": "local"},
        "git": {"autocheckpoint": False},
        "wiki": {"enabled": True},
        "observability": {"transcript": True},
        "context": {"repo_map": "tool", "repo_map_tokens": 1024},
        "providers": [{"name": "openrouter", "type": "openrouter", "key_ok": True}],
        "extensions": {
            "counts": {
                "skills": 0,
                "commands": 0,
                "subagents": 0,
                "hooks": 0,
                "mcp_servers": 0,
                "async_subagents": 0,
            }
        },
        "errors": [],
        "warnings": [],
    }
    diag.update(overrides)
    return diag


@pytest.mark.parametrize("locale", ["en", "th"])
def test_doctor_headings_localize(locale: str) -> None:
    body = _body(_min_diag(), locale)
    assert t("doctor.title", locale) in body
    assert t("doctor.section.providers", locale) in body
    assert t("doctor.section.extensions", locale) in body
    assert t("doctor.label.model", locale) in body
    assert t("doctor.label.git", locale) in body


@pytest.mark.parametrize("locale", ["en", "th"])
def test_doctor_pass_fail_labels_localize(locale: str) -> None:
    body = _body(_min_diag(), locale)
    assert t("doctor.status.key_ok", locale) in body
    assert t("doctor.cta.ok", locale) in body
    assert t("doctor.status.ok", locale) in body


def test_thai_and_english_chrome_differ() -> None:
    en = _body(_min_diag(), "en")
    th = _body(_min_diag(), "th")
    assert t("doctor.section.providers", "en") in en
    assert t("doctor.section.providers", "th") in th
    assert t("doctor.section.providers", "th") not in en
    assert t("doctor.status.key_ok", "en") in en
    assert t("doctor.status.key_ok", "th") in th
    assert t("doctor.cta.ok", "en") in en
    assert t("doctor.cta.ok", "th") in th


def test_human_output_has_no_argparse_brick() -> None:
    body = _body(_min_diag(), "en")
    assert "git.autocheckpoint" not in body
    assert "observability.transcript" not in body
    assert "context.repo_map" not in body
    assert "wiki.enabled" not in body
    assert "Main model build" not in body
    assert "Actionable errors" not in body
    assert "--json" not in body
    assert "usage:" not in body.lower()


def test_missing_config_localizes() -> None:
    diag = _min_diag(global_config_present=False, ok=False)
    assert t("doctor.no_config", "en") in _body(diag, "en")
    assert t("doctor.no_config", "th") in _body(diag, "th")
    assert "jarn setup" in _body(diag, "th")


def test_errors_section_is_short_and_localized() -> None:
    diag = _min_diag(
        ok=False,
        errors=[
            {
                "code": "JARN-DOCTOR-001",
                "summary": "Configuration could not be loaded.",
                "cause": "bad yaml",
                "component": "configuration",
                "retryable": False,
                "action": "Run jarn doctor again.",
                "log_path": "/tmp/jarn.log",
            }
        ],
    )
    en = _body(diag, "en")
    th = _body(diag, "th")
    assert t("doctor.section.errors", "en") in en
    assert t("doctor.section.errors", "th") in th
    assert "Actionable errors" not in en
    assert t("error.cause", "th") in th
    assert t("doctor.cta.issues", "en", n=1) in en


def test_json_keys_stay_english() -> None:
    diag = _min_diag(
        git={"autocheckpoint": True},
        wiki={"enabled": False},
        observability={"transcript": True},
        context={"repo_map": "auto", "repo_map_tokens": 2048},
    )
    payload = json.loads(doctor_to_json(diag))
    assert payload["git"]["autocheckpoint"] is True
    assert payload["wiki"]["enabled"] is False
    assert payload["observability"]["transcript"] is True
    assert payload["context"]["repo_map"] == "auto"
    assert payload["main_model_builds"] is True
    assert payload["global_config"] == "/tmp/config.yaml"
    dumped = json.dumps(payload)
    assert "ผู้ให้บริการ" not in dumped
    assert "ผ่านหมด" not in dumped
    assert "key_ok" in dumped


def test_render_does_not_mutate_diag_or_json() -> None:
    diag = _min_diag()
    before = json.dumps(diag, sort_keys=True)
    doctor_lines(diag, locale="th")
    assert json.dumps(diag, sort_keys=True) == before
    assert json.loads(doctor_to_json(diag))["global_config"] == "/tmp/config.yaml"


def test_peek_global_ui_locale_th_when_lang_is_english(
    isolated_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_home / "config.yaml").write_text("ui:\n  locale: th\n", encoding="utf-8")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    body = _plain("\n".join(doctor_lines(_min_diag())))
    assert t("doctor.section.providers", "th") in body
    assert t("doctor.status.key_ok", "th") in body
