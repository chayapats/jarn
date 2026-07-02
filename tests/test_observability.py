"""Observability wiring — logging and tracing."""

from __future__ import annotations

import logging
import os

from jarn.config.schema import ObservabilityConfig, TracingConfig
from jarn.observability.logging import setup_logging
from jarn.observability.tracing import configure_langsmith, configure_otel, configure_tracing


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


def test_configure_tracing_langsmith_backend(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    obs = ObservabilityConfig(langsmith=True, tracing=TracingConfig(backend="langsmith"))
    assert configure_tracing(obs) is True


def test_otel_backend():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    assert configure_otel(span_processor=processor) is True

    from opentelemetry import trace

    tracer = trace.get_tracer("jarn.test")
    with tracer.start_as_current_span("test-span"):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "test-span"
