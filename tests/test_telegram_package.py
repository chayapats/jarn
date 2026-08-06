"""T-TG-1: telegram package import guard + doctor configured-but-uninstalled."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarn.doctor.telegram_extra import (
    TELEGRAM_EXTRA_MISSING,
    gateway_enabled,
    telegram_extra_warnings,
)


def _block_aiogram(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``import aiogram`` to raise ImportError (extra not installed)."""
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "aiogram" or name.startswith("aiogram."):
            raise ImportError("No module named 'aiogram'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)
    sys.modules.pop("aiogram", None)


def test_require_aiogram_raises_clear_error_when_missing(monkeypatch):
    _block_aiogram(monkeypatch)
    from jarn.telegram import require_aiogram

    with pytest.raises(ImportError, match="jarn\\[telegram\\]") as excinfo:
        require_aiogram()
    assert "configured-but-uninstalled" in str(excinfo.value)


def test_telegram_aiogram_getattr_guard(monkeypatch):
    _block_aiogram(monkeypatch)
    import jarn.telegram as tg

    with pytest.raises(ImportError, match="telegram"):
        _ = tg.aiogram


def test_gateway_enabled_reads_raw_yaml(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("gateway:\n  enabled: true\n", encoding="utf-8")
    assert gateway_enabled(None, global_config_path=cfg_path) is True

    cfg_path.write_text("gateway:\n  enabled: false\n", encoding="utf-8")
    assert gateway_enabled(None, global_config_path=cfg_path) is False

    cfg_path.write_text("providers: {}\n", encoding="utf-8")
    assert gateway_enabled(None, global_config_path=cfg_path) is False


def test_gateway_enabled_prefers_typed_config(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("gateway:\n  enabled: false\n", encoding="utf-8")
    typed = SimpleNamespace(gateway=SimpleNamespace(enabled=True))
    assert gateway_enabled(typed, global_config_path=cfg_path) is True


def test_doctor_warns_configured_but_uninstalled(monkeypatch, tmp_path: Path):
    _block_aiogram(monkeypatch)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("gateway:\n  enabled: true\n", encoding="utf-8")

    warnings = telegram_extra_warnings(None, global_config_path=cfg_path)
    assert warnings == [TELEGRAM_EXTRA_MISSING]
    assert "configured-but-uninstalled" in warnings[0]
    assert "jarn[telegram]" in warnings[0]


def test_doctor_silent_when_gateway_disabled(monkeypatch, tmp_path: Path):
    _block_aiogram(monkeypatch)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("gateway:\n  enabled: false\n", encoding="utf-8")
    assert telegram_extra_warnings(None, global_config_path=cfg_path) == []


def test_doctor_silent_when_aiogram_present(monkeypatch, tmp_path: Path):
    fake = SimpleNamespace(__name__="aiogram")
    monkeypatch.setitem(sys.modules, "aiogram", fake)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("gateway:\n  enabled: true\n", encoding="utf-8")
    assert telegram_extra_warnings(None, global_config_path=cfg_path) == []


def test_collect_doctor_surfaces_gateway_warning(monkeypatch, tmp_path: Path):
    """collect_doctor includes the configured-but-uninstalled warning.

    Pass a pre-built Config so load_config never sees the ``gateway:`` key
    (schema may still forbid extras). The probe reads raw YAML from gpath.
    """
    from jarn.config import paths
    from jarn.config.schema import Config
    from jarn.doctor.collect import collect_doctor

    _block_aiogram(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    gp = home / "config.yaml"
    gp.write_text(
        "providers:\n  openrouter:\n    type: openrouter\n    api_key: sk-test\n"
        "    base_url: https://openrouter.ai/api/v1\n"
        "gateway:\n  enabled: true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    diag: dict = {}
    collect_doctor(diag, config=Config(), project_root=None, project_trusted=True)

    assert diag.get("gateway", {}).get("enabled") is True
    warnings = diag.get("gateway", {}).get("warnings") or []
    assert any("configured-but-uninstalled" in w for w in warnings)
    assert TELEGRAM_EXTRA_MISSING in (diag.get("warnings") or [])
