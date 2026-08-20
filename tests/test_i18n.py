"""S01 — UI chrome i18n catalog and ``ui.locale``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jarn.config import settings
from jarn.config.defaults import global_config_template
from jarn.config.loader import ConfigError, load_config
from jarn.config.schema import UIConfig
from jarn.tui.i18n import CATALOGS, EN, TH, resolve_locale, t


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalogs_share_every_seeded_key():
    assert set(EN) == set(TH) == set(CATALOGS["en"]) == set(CATALOGS["th"])
    assert EN.keys()


@pytest.mark.parametrize("locale", ["en", "th"])
def test_every_seeded_key_has_nonempty_copy(locale):
    catalog = CATALOGS[locale]
    empty = [key for key, value in catalog.items() if not value.strip()]
    assert empty == [], f"empty {locale} catalog values: {empty}"


def test_catalogs_are_frozen():
    with pytest.raises(TypeError):
        EN["composer.placeholder.first"] = "nope"  # type: ignore[index]
    with pytest.raises(TypeError):
        TH["composer.placeholder.first"] = "nope"  # type: ignore[index]


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("th", "ให้ jarn วางแผน ค้นหา หรือลงมือ"),
        ("en", "Ask jarn to plan, search, or build"),
    ],
)
def test_first_turn_placeholder(locale, expected):
    assert t("composer.placeholder.first", locale) == expected


@pytest.mark.parametrize(
    ("key", "en", "th"),
    [
        ("composer.placeholder.later", "Message jarn", "พิมพ์ถึง jarn"),
        ("thinking.plain", "Thinking…", "คิด…"),
        ("tool.verb.read_file", "Read", "อ่าน"),
        ("tool.verb.edit_file", "Edit", "แก้"),
        ("tool.verb.write_file", "Write", "เขียน"),
        ("tool.verb.bash", "Run", "รัน"),
        (
            "splash.orientation",
            "Type a message. /help for commands.",
            "พิมพ์ข้อความได้เลย  ·  /help สำหรับคำสั่ง",
        ),
        ("approval.once", "Allow once", "ครั้งนี้ครั้งเดียว"),
        ("approval.session", "Allow for this session", "ทั้งเซสชันนี้"),
        ("approval.always", "Always allow", "อนุญาตการแก้ไฟล์นี้ตลอด"),
        ("approval.deny", "Deny", "ปฏิเสธ"),
        ("approval.danger", "Dangerous", "อันตราย"),
        ("approval.edit", "Edit before apply", "แก้ก่อนแล้วค่อยใช้"),
        (
            "yolo.confirm",
            "YOLO will stop asking before edits and shell. The danger-guard still blocks.",
            "YOLO จะเลิกถามก่อนแก้ไฟล์และรันคำสั่ง อันตรายยังถูกบล็อก",
        ),
        ("error.next", "Next", "ถัดไป"),
    ],
)
def test_seeded_chrome_strings(key, en, th):
    assert t(key, "en") == en
    assert t(key, "th") == th


def test_format_kwargs():
    assert t("tool.result.lines", "en", n=42) == "42 lines"
    assert t("tool.result.lines", "th", n=42) == "42 บรรทัด"
    assert t("approval.header.edit", "th", object="src/auth/session.py") == (
        "อนุญาตให้แก้ src/auth/session.py?"
    )
    assert t("approval.header.write", "en", object="notes.txt") == (
        "Allow this write to notes.txt?"
    )
    assert t("approval.header.network", "th", object="api.example") == (
        "อนุญาตให้เชื่อมต่อ api.example?"
    )


def test_missing_key_raises():
    with pytest.raises(KeyError, match="missing i18n key"):
        t("no.such.key", "en")
    with pytest.raises(KeyError, match="missing i18n key"):
        t("no.such.key", "th")


def test_unknown_locale_raises():
    with pytest.raises(ValueError, match="unknown locale"):
        t("composer.placeholder.first", "fr")


# ---------------------------------------------------------------------------
# resolve_locale
# ---------------------------------------------------------------------------


def test_resolve_locale_explicit_overrides_environ():
    cfg = SimpleNamespace(ui=SimpleNamespace(locale="th"))
    assert resolve_locale(cfg, {"LANG": "en_US.UTF-8"}) == "th"
    cfg.ui.locale = "en"
    assert resolve_locale(cfg, {"LANG": "th_TH.UTF-8"}) == "en"


def test_resolve_locale_auto_th_from_lang():
    assert resolve_locale("auto", {"LANG": "th_TH.UTF-8"}) == "th"


def test_resolve_locale_auto_en_from_lang():
    assert resolve_locale("auto", {"LANG": "en_US.UTF-8"}) == "en"


def test_resolve_locale_auto_empty_environ_is_en():
    assert resolve_locale("auto", {}) == "en"


def test_resolve_locale_lc_all_overrides_lang():
    assert (
        resolve_locale(
            "auto",
            {"LC_ALL": "en_US.UTF-8", "LANG": "th_TH.UTF-8"},
        )
        == "en"
    )


def test_resolve_locale_lc_messages_when_lc_all_unset():
    assert (
        resolve_locale(
            "auto",
            {"LC_MESSAGES": "th_TH.UTF-8", "LANG": "en_US.UTF-8"},
        )
        == "th"
    )


def test_resolve_locale_rejects_unknown_setting():
    with pytest.raises(ValueError, match="ui.locale"):
        resolve_locale("fr", {})


# ---------------------------------------------------------------------------
# ui.locale config
# ---------------------------------------------------------------------------


def test_ui_config_locale_defaults_to_auto():
    assert UIConfig().locale == "auto"


def test_loader_locale_default_is_auto(tmp_path):
    cfg = load_config(global_path=tmp_path / "missing.yaml", project_path=None)
    assert cfg.ui.locale == "auto"


@pytest.mark.parametrize("value", ["auto", "en", "th"])
def test_loader_locale_accepted(tmp_path, value):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"locale": value}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.ui.locale == value


def test_loader_locale_invalid_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"locale": "fr"}})
    with pytest.raises(ConfigError, match="ui.locale"):
        load_config(global_path=gp, project_path=None)


def test_ui_locale_is_settable():
    assert settings.is_settable("ui.locale")
    assert settings.coerce("ui.locale", "th") == "th"
    with pytest.raises(settings.SettingError):
        settings.coerce("ui.locale", "fr")


def test_defaults_template_includes_locale():
    template = global_config_template()
    assert "locale:" in template
    assert "auto | en | th" in template


def test_help_catalog_covers_every_command():
    from jarn.commands.registry import COMMAND_SPECS, help_blurb_key, help_description_key

    missing = []
    drifted = []
    untranslated = []
    for spec in COMMAND_SPECS:
        key = help_description_key(spec.name)
        en = t(key, "en")
        th = t(key, "th")
        if en != spec.description:
            drifted.append((spec.name, "description", en, spec.description))
        if en == th:
            untranslated.append(key)
        blurb_key = help_blurb_key(spec.name)
        if spec.blurb != spec.description and blurb_key not in EN:
            missing.append(blurb_key)
        if blurb_key in EN:
            blurb_en = t(blurb_key, "en")
            blurb_th = t(blurb_key, "th")
            if blurb_en != spec.blurb:
                drifted.append((spec.name, "blurb", blurb_en, spec.blurb))
            if blurb_en == blurb_th:
                untranslated.append(blurb_key)
    assert drifted == []
    assert untranslated == []
    assert missing == []
