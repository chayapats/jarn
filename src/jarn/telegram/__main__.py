"""Thin ``python -m jarn.telegram`` entry (T-TG-2 stub; full CLI is T-OPS-2).

Requires ``gateway:`` configured in the global config and the ``telegram`` extra.
Uses :class:`~jarn.telegram.backend.InMemoryGatewayBackend` only when
``JARN_TELEGRAM_FAKE_BACKEND=1`` (tests/smoke); otherwise refuses to start
without a real daemon backend wired by the operator/daemon package.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from jarn.telegram import require_aiogram

        require_aiogram()
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    token = os.environ.get("JARN_TELEGRAM_BOT_TOKEN", "").strip()
    allowed_raw = os.environ.get("JARN_TELEGRAM_ALLOWED_USER_IDS", "").strip()
    allowed: list[int] = []
    if allowed_raw:
        for part in allowed_raw.split(","):
            part = part.strip()
            if part:
                allowed.append(int(part))

    # Prefer loaded config when present.
    try:
        from jarn.config.loader import load_config

        cfg = load_config()
        tg = cfg.gateway.telegram
        if not token:
            token = (tg.token or "").strip()
        if not allowed:
            allowed = list(tg.allowed_user_ids)
        if not cfg.gateway.enabled:
            print(
                "gateway.enabled is false — enable it in ~/.jarn/config.yaml",
                file=sys.stderr,
            )
            return 2
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("jarn.telegram").warning(
            "config load skipped (%s); using env only", exc
        )

    if not token:
        print(
            "Missing bot token. Set gateway.telegram.token or "
            "JARN_TELEGRAM_BOT_TOKEN.",
            file=sys.stderr,
        )
        return 2
    if not allowed:
        print(
            "gateway.telegram.allowed_user_ids is empty — deny-by-default; "
            "refusing to start.",
            file=sys.stderr,
        )
        return 2

    if os.environ.get("JARN_TELEGRAM_FAKE_BACKEND") == "1":
        from jarn.telegram.backend import InMemoryGatewayBackend

        backend = InMemoryGatewayBackend()
    else:
        print(
            "No GatewayBackend wired yet (daemon package owns that). "
            "Set JARN_TELEGRAM_FAKE_BACKEND=1 for a smoke-only in-memory backend, "
            "or start via the gateway daemon once T-DMN lands.",
            file=sys.stderr,
        )
        return 2

    from jarn.telegram.bot import run_gateway_bot

    return asyncio.run(
        run_gateway_bot(token=token, allowed_user_ids=allowed, backend=backend)
    )


if __name__ == "__main__":
    raise SystemExit(main())
