"""Backward-compatible re-exports — prefer :mod:`jarn.doctor`."""

from jarn.doctor.extensions import MarkdownExtensionRow, collect_extensions

__all__ = ["MarkdownExtensionRow", "collect_extensions"]
