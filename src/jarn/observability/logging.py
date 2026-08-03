"""Local structured logging to ``~/.jarn/logs/jarn.log`` (rotating)."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO

from jarn.config import paths
from jarn.config.secrets import redact_secrets

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


@contextlib.contextmanager
def _interprocess_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive OS lock on *path* for the duration of one log emit."""
    lock_file: BinaryIO = path.open("a+b")
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(  # type: ignore[attr-defined]
                        lock_file.fileno(), msvcrt.LK_NBLCK, 1  # type: ignore[attr-defined]
                    )
                    break
                except OSError:
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            lock_file.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


class ConcurrentRotatingFileHandler(RotatingFileHandler):
    """A ``RotatingFileHandler`` safe for Jarn's multi-process log file.

    The standard handler protects threads in one process only. Every CLI process
    otherwise races the shared rename/remove rollover chain. Serializing the whole
    emit makes the size check, optional rollover, and write atomic across processes.
    ``delay=True`` plus closing after every write ensures no process retains a stale
    handle to a file another process has renamed (and permits renames on Windows).
    """

    def __init__(self, filename: str | Path, **kwargs) -> None:
        kwargs["delay"] = True
        super().__init__(filename, **kwargs)
        self.lock_path = Path(f"{self.baseFilename}.lock")

    def emit(self, record: logging.LogRecord) -> None:
        with _interprocess_lock(self.lock_path):
            try:
                super().emit(record)
            finally:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None


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

    handler = ConcurrentRotatingFileHandler(
        logs_dir / "jarn.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger
