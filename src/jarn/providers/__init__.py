"""Provider & model-routing subsystem."""

from jarn.providers.models import (
    DEMO_PROFILE,
    ModelFactory,
    ModelRef,
    ModelResolutionError,
    build_demo_model,
    demo_provider_config,
    is_demo_active,
    list_remote_models,
    parse_model_ref,
    qualify_model_ref,
    remote_context_window,
    strip_profile,
    suggest_slug,
)

__all__ = [
    "DEMO_PROFILE",
    "ModelFactory",
    "ModelRef",
    "ModelResolutionError",
    "build_demo_model",
    "demo_provider_config",
    "is_demo_active",
    "list_remote_models",
    "parse_model_ref",
    "qualify_model_ref",
    "remote_context_window",
    "strip_profile",
    "suggest_slug",
]
