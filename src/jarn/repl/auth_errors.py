"""Auth-error helpers for the REPL turn loop."""

from __future__ import annotations

from rich.markup import escape as _rich_escape

from jarn.tui import palette
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
    who = f"for {provider} " if provider else ""
    head = (
        f"[{palette.C_ERROR}]Your API key {who}was rejected (401).[/{palette.C_ERROR}] "
        f"Fix it with [{palette.C_NOTICE}]/key[/{palette.C_NOTICE}], run "
        f"[{palette.C_NOTICE}]jarn setup[/{palette.C_NOTICE}], or set the provider's "
        f"API-key env var."
    )
    detail = raw.strip()
    if detail:
        head += f"\n[{palette.C_DIM}]{_rich_escape(detail)}[/{palette.C_DIM}]"
    return head
