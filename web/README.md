# web/ — Web UI design reference (NOT a shipped feature)

This directory is a **design sketch**, not a working server. The Web UI is a
post-launch item (see [../docs/WEB_UI.md](../docs/WEB_UI.md)). Nothing here is wired
into the `jarn` package, installed, or tested.

It exists to demonstrate that the agent core is UI-agnostic: a future FastAPI +
WebSocket server can reuse `jarn.tui.controller.Controller` and
`jarn.agent.session.SessionDriver` directly, mapping the driver's typed `Event`
stream to WebSocket frames and the `approver` callback to a browser round-trip.

See `server_sketch.py` for the shape of that integration.

When the Web UI is actually built, it will live in its own package (likely a
Next.js frontend + a thin FastAPI backend) and the premium/hosted pieces will be
governed by the [open-core model](../docs/OPEN_CORE.md).
