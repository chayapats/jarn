"""Telegram gateway transport — optional ``jarn[telegram]`` extra.

The package itself always ships with jarn; only the ``aiogram`` dependency is
gated behind the ``telegram`` extra. Importing this package (or calling
:func:`require_aiogram`) fails clearly when the extra is missing.

Submodules (``bot``, ``outbox``, ``inbound_media``, …) import aiogram lazily via
:func:`require_aiogram` / Bot construction so ``import jarn.telegram`` stays
safe without the extra.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "require_aiogram",
    "GatewayBackend",
    "InMemoryGatewayBackend",
]


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
    """Lazy exports — keep ``import jarn.telegram`` free of aiogram/daemon deps."""
    if name == "aiogram":
        return require_aiogram()
    if name == "GatewayBackend":
        from jarn.telegram.backend import GatewayBackend

        return GatewayBackend
    if name == "InMemoryGatewayBackend":
        from jarn.telegram.backend import InMemoryGatewayBackend

        return InMemoryGatewayBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
