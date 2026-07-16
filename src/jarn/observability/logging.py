"""Local structured logging to ``~/.jarn/logs/jarn.log`` (rotating)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from jarn.config import paths
from jarn.config.secrets import redact_secrets

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class RedactingFilter(logging.Filter):
    """Scrub secret-shaped substrings from every log record before it is emitted.

    A resolved API key that leaks into a log line (via an interpolated ``{exc}``
    or a debug dump) would persist to ``jarn.log`` indefinitely. This filter
    formats the record, runs it through the central redactor, and writes the
    redacted string back onto the record so the handler's formatter emits the
    scrubbed version. It is best-effort, matching the transcript redactor.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            formatted = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging itself crash a turn
            # A record whose interpolation raised is the most likely to carry an
            # unredacted secret in its raw msg/args; suppress rather than emit it.
            record.msg = "<unformattable log record - suppressed for redaction safety>"
            record.args = ()
            return True
        record.msg = redact_secrets(formatted)
        record.args = ()
        return True


def setup_logging(level: str = "info") -> logging.Logger:
    """Configure the ``jarn`` logger to write to the rotating log file.

    TUI apps must not log to stdout/stderr (it corrupts the display), so this
    attaches only a file handler. Returns the configured root ``jarn`` logger.
    """
    logs_dir = paths.global_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("jarn")
    logger.setLevel(_LEVELS.get(level.lower(), logging.INFO))
    logger.propagate = False

    # Avoid duplicate handlers on repeated setup (e.g. tests).
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return logger

    handler = RotatingFileHandler(
        logs_dir / "jarn.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger
