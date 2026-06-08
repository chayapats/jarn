"""DESIGN SKETCH — not a working server, not part of the jarn package.

Shows how a future web backend would reuse the existing UI-agnostic core. This is
illustrative pseudocode-grade Python: it references FastAPI/uvicorn which are NOT
dependencies of jarn. Do not import this module from the package or tests.

See ../docs/WEB_UI.md for the full plan.
"""

from __future__ import annotations

# NOTE: these imports are real and would work; the FastAPI bits are illustrative.
from jarn.agent.session import ApprovalReply, ApprovalRequest, EventKind
from jarn.config.loader import load_config
from jarn.tui.controller import Controller


async def run_turn_over_websocket(ws, controller: Controller, text: str) -> None:
    """Map a SessionDriver turn onto a WebSocket connection.

    The TUI and a web client share this exact flow — only the transport differs.
    """

    async def approver(request: ApprovalRequest) -> ApprovalReply:
        # Round-trip the approval to the browser and await the user's choice.
        await ws.send_json({
            "type": "approval",
            "action": request.action.kind.value,
            "target": request.action.target,
            "dangerous": request.result.dangerous,
            "reason": request.result.reason,
        })
        reply = await ws.receive_json()
        from jarn.permissions import RememberScope
        return ApprovalReply(
            approved=bool(reply.get("approved")),
            scope=RememberScope(reply.get("scope", "once")),
        )

    await controller.ensure_runtime()
    driver = controller.make_driver(approver)
    async for event in driver.run_turn(text):
        await ws.send_json({"type": event.kind.value, "text": event.text, "data": event.data})
        if event.kind is EventKind.DONE:
            await ws.send_json({"type": "status", "text": controller.status_line})


def build_app():
    """Illustrative FastAPI wiring (requires fastapi/uvicorn — not jarn deps)."""
    from fastapi import FastAPI, WebSocket  # type: ignore

    app = FastAPI()
    config = load_config()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):  # pragma: no cover - sketch only
        await ws.accept()
        controller = Controller(config, project_root=None)
        try:
            while True:
                msg = await ws.receive_json()
                await run_turn_over_websocket(ws, controller, msg["text"])
        finally:
            controller.close()

    return app
