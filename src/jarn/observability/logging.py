"""Local structured logging to ``~/.jarn/logs/jarn.log`` (rotating)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from jarn.config import paths

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


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
    logger.addHandler(handler)
    return logger
