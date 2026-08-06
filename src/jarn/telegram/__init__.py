"""Telegram gateway transport — optional ``jarn[telegram]`` extra.

The package itself always ships with jarn; only the ``aiogram`` dependency is
gated behind the ``telegram`` extra. Importing this package (or calling
:func:`require_aiogram`) fails clearly when the extra is missing.
"""

from __future__ import annotations

from typing import Any

__all__ = ["require_aiogram"]

_AIOGRAM_MISSING = (
    "The Telegram gateway requires the 'telegram' extra "
    "(configured-but-uninstalled). Install it with: pip install 'jarn[telegram]'"
)


def require_aiogram() -> Any:
    """Import and return ``aiogram``, or raise a clear :class:`ImportError`.

    Prefer this helper over a bare ``import aiogram`` so missing-extra failures
    point operators at ``pip install 'jarn[telegram]'``.
    """
    try:
        import aiogram
    except ImportError as exc:  # pragma: no cover - exercised via mocked import
        raise ImportError(_AIOGRAM_MISSING) from exc
    return aiogram


def __getattr__(name: str) -> Any:
    """Lazy-import ``aiogram`` when accessed as ``jarn.telegram.aiogram``."""
    if name == "aiogram":
        return require_aiogram()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
