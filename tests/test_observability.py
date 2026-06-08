"""Observability wiring — logging and LangSmith tracing."""

from __future__ import annotations

import logging
import os

from jarn.observability.logging import setup_logging
from jarn.observability.tracing import configure_langsmith


def test_setup_logging_writes_to_file(isolated_home):
    logger = setup_logging("debug")
    assert logger.name == "jarn"
    assert logger.level == logging.DEBUG
    assert any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers
    )
    # Repeated setup must not duplicate handlers.
    setup_logging("info")
    handlers = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(handlers) == 1


def test_configure_langsmith_disabled(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert configure_langsmith(False) is False
    assert os.environ.get("LANGSMITH_TRACING") is None


def test_configure_langsmith_enabled_without_key(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    assert configure_langsmith(True) is False


def test_configure_langsmith_enabled_with_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert configure_langsmith(True, project="myproj") is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_PROJECT"] == "myproj"
