"""Doctor diagnostics: collection and rendering."""

from jarn.doctor.collect import collect_doctor
from jarn.doctor.extensions import MarkdownExtensionRow, collect_extensions
from jarn.doctor.render import (
    append_extension_lines,
    doctor_lines,
    doctor_to_json,
    render_doctor_console,
)
from jarn.doctor.telegram_extra import TELEGRAM_EXTRA_MISSING, telegram_extra_warnings

__all__ = [
    "MarkdownExtensionRow",
    "TELEGRAM_EXTRA_MISSING",
    "append_extension_lines",
    "collect_doctor",
    "collect_extensions",
    "doctor_lines",
    "doctor_to_json",
    "render_doctor_console",
    "telegram_extra_warnings",
]
