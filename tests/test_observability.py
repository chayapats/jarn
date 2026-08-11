"""Observability wiring — logging and tracing."""

from __future__ import annotations

import logging
import multiprocessing
import os

from jarn.config.schema import ObservabilityConfig, TracingConfig
from jarn.observability.logging import (
    ConcurrentRotatingFileHandler,
    RedactingFilter,
    setup_logging,
)
from jarn.observability.tracing import (
    configure_langsmith,
    configure_otel,
    configure_tracing,
    span,
)


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


def test_file_log_redacts_exact_resolved_custom_credential(tmp_path, monkeypatch):
    import jarn.config.secrets as secrets

    credential = "arbitrary-provider-key-7q2"
    path = tmp_path / "jarn.log"
    logger = logging.getLogger(f"jarn.test.exact-redaction.{id(path)}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = ConcurrentRotatingFileHandler(
        path, maxBytes=20_000, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    secrets._clear_resolved_secrets_for_testing()
    try:
        monkeypatch.setenv("CUSTOM_LOGGING_KEY", credential)
        assert secrets.resolve("${CUSTOM_LOGGING_KEY}") == credential
        logger.error("provider failed with %s", credential)
        handler.close()

        raw = path.read_bytes()
        assert credential.encode() not in raw
        assert b"[REDACTED]" in raw
    finally:
        logger.removeHandler(handler)
        handler.close()
        secrets._clear_resolved_secrets_for_testing()


def _write_rotating_log(path: str, worker: int) -> None:
    """Child-process target for the concurrent rollover regression test."""
    logger = logging.getLogger(f"jarn-rollover-{os.getpid()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = ConcurrentRotatingFileHandler(
        path, maxBytes=25_000, backupCount=8, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    for line in range(400):
        logger.info("P%02d-%04d %s", worker, line, "x" * 64)
    handler.close()


def test_concurrent_log_rollover_preserves_every_line(tmp_path):
    """Regression for #58: separate processes cannot race the rollover chain."""
    path = tmp_path / "jarn.log"
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_write_rotating_log, args=(str(path), i)) for i in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    actual: set[str] = set()
    for log_path in tmp_path.glob("jarn.log*"):
        if log_path.suffix == ".lock":
            continue
        for text in log_path.read_text(encoding="utf-8").splitlines():
            actual.add(text.split(maxsplit=1)[0])
    expected = {f"P{worker:02d}-{line:04d}" for worker in range(4) for line in range(400)}
    assert actual == expected


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


def test_span_is_no_op_safe():
    """span() is usable as a context manager even with no tracer configured."""
    with span("standalone", detail="x") as s:
        # Yields either a live span or None; never raises regardless of backend.
        assert s is None or s is not None


def test_span_emits_named_span_with_string_attrs():
    """With a test span_processor, span() produces a named span and stringifies attrs."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # The global TracerProvider is set-once per process; reset the guard so this
    # test installs its own in-memory provider regardless of test ordering.
    trace._TRACER_PROVIDER_SET_ONCE = trace.Once()
    trace._TRACER_PROVIDER = None

    exporter = InMemorySpanExporter()
    assert configure_otel(span_processor=SimpleSpanProcessor(exporter)) is True

    with span("jarn.turn", turn=3, mode="ask"):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "jarn.turn"
    # Attributes are coerced to strings.
    assert spans[0].attributes["turn"] == "3"
    assert spans[0].attributes["mode"] == "ask"
