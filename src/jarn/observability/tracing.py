"""Opt-in LangSmith tracing.

When enabled in config, set the environment variables LangChain/LangGraph read
to emit traces. We only *enable*; we never disable a user's pre-set tracing.
Telemetry (separate, local-only usage analytics) is implemented under
``observability.telemetry`` (numeric turn events, default OFF); only a *remote*
telemetry sink is out of scope for v1 — see ROADMAP.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("jarn.tracing")


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
