"""Auth-error helpers for the REPL turn loop."""

from __future__ import annotations

from jarn.tui import layout
from jarn.tui.controller import Controller


def _provider_hint(controller: Controller) -> str:
    """Provider/profile name for the active main model, for auth-error messages.

    Best-effort fallback used when the failing turn's ERROR event didn't already
    carry a ``provider`` (e.g. the driver had no resolved model ref). Returns ``""``
    when it can't be determined so the message degrades to a generic phrasing."""
    ref = controller.config.resolved_main_model() or ""
    return ref.split("/", 1)[0] if "/" in ref else ""


def _friendly_auth_error(raw: str, provider: str) -> str:
    """Map a provider 401/auth rejection to a friendly, actionable message.

    The raw SDK detail (e.g. ``Error code: 401 - {...invalid x-api-key...}``) is
    unhelpful on its own, so we name the provider and the concrete next steps and
    keep the original text available, but dim. ``provider`` may be empty, in which
    case we fall back to a generic "API key" phrasing.
    """
    if provider == "codex_subscription":
        head = (
            layout.err("Your ChatGPT subscription is not connected to Codex.")
            + " Run "
            + layout.notice("jarn codex login")
            + ", then verify it with "
            + layout.notice("jarn codex status")
            + "."
        )
    else:
        who = f"for {provider} " if provider else ""
        head = (
            layout.err(f"Your API key {who}was rejected (401).")
            + " Fix it with "
            + layout.notice("/key")
            + ", run "
            + layout.notice("jarn setup")
            + ", or set the provider's API-key env var."
        )
    detail = raw.strip()
    if detail:
        head += "\n" + layout.muted(detail)
    return head
