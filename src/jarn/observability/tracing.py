"""Opt-in tracing — LangSmith (default) or OpenTelemetry.

When enabled in config, set the environment variables LangChain/LangGraph read
to emit traces (LangSmith) or configure the OTel SDK (``otel`` backend).
We only *enable*; we never disable a user's pre-set tracing.
Telemetry (separate, local-only usage analytics) is implemented under
``observability.telemetry`` (numeric turn events, default OFF); only a *remote*
telemetry sink is out of scope for v1 — see ROADMAP.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarn.config.schema import ObservabilityConfig

logger = logging.getLogger("jarn.tracing")

_VALID_BACKENDS = frozenset({"langsmith", "otel"})


def configure_langsmith(enabled: bool, *, project: str = "jarn") -> bool:
    """Enable LangSmith tracing if ``enabled`` and an API key is available.

    Returns True if tracing was turned on. Requires ``LANGSMITH_API_KEY`` (or
    legacy ``LANGCHAIN_API_KEY``) to be present in the environment.
    """
    if not enabled:
        return False
    api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    if not api_key:
        logger.warning("LangSmith tracing requested but no LANGSMITH_API_KEY set.")
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGSMITH_PROJECT", project)
    logger.info("LangSmith tracing enabled (project=%s)", project)
    return True


def configure_otel(
    *,
    service_name: str = "jarn",
    span_processor: Any | None = None,
) -> bool:
    """Configure the OpenTelemetry SDK tracer provider.

    By default exports to ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or localhost:4318).
    Pass ``span_processor`` to inject a test processor (e.g. in-memory exporter).
    Requires the ``jarn[otel]`` extra (``opentelemetry-sdk``).
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTel tracing requested but opentelemetry-sdk is not installed "
            "(pip install 'jarn[otel]')."
        )
        return False

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    if span_processor is not None:
        provider.add_span_processor(span_processor)
    else:
        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/traces"
        )
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info("OpenTelemetry tracing enabled (service=%s)", service_name)
    return True


def configure_tracing(obs: ObservabilityConfig, *, project: str = "jarn") -> bool:
    """Enable tracing per ``observability.tracing.backend`` and related flags."""
    backend = obs.tracing.backend
    if backend not in _VALID_BACKENDS:
        logger.warning("Unknown observability.tracing.backend %r — tracing disabled.", backend)
        return False
    if backend == "otel":
        return configure_otel()
    return configure_langsmith(obs.langsmith, project=project)
