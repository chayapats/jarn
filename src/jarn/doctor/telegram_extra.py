"""Doctor probe: ``gateway:`` configured but ``telegram`` extra missing.

Prefers a typed ``config.gateway.enabled`` when True; otherwise falls back to
raw global YAML so a default ``Config()`` (enabled=False) cannot mask a
configured-but-unloaded ``gateway:`` block. Fail-soft on missing/unreadable
files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

TELEGRAM_EXTRA_MISSING = (
    "gateway: configured but telegram extra not installed "
    "(configured-but-uninstalled) — pip install 'jarn[telegram]'"
)


def gateway_enabled(
    config: Any = None,
    *,
    global_config_path: Path | None = None,
) -> bool:
    """Return True when gateway is enabled in typed config or raw YAML."""
    gateway = getattr(config, "gateway", None) if config is not None else None
    if gateway is not None and bool(getattr(gateway, "enabled", False)):
        return True

    if global_config_path is None or not global_config_path.is_file():
        return False
    try:
        from jarn.config.loader import _read_yaml

        raw = _read_yaml(global_config_path)
    except Exception:
        return False
    section = raw.get("gateway")
    if not isinstance(section, dict):
        return bool(section) if section is not None else False
    return bool(section.get("enabled"))


def aiogram_installed() -> bool:
    """Return True when ``aiogram`` can be imported."""
    try:
        import aiogram  # noqa: F401
    except ImportError:
        return False
    return True


def telegram_extra_warnings(
    config: Any = None,
    *,
    global_config_path: Path | None = None,
) -> list[str]:
    """Warn when gateway is enabled but the ``telegram`` extra is missing."""
    if not gateway_enabled(config, global_config_path=global_config_path):
        return []
    if aiogram_installed():
        return []
    return [TELEGRAM_EXTRA_MISSING]
