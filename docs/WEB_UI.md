# Web UI (planned — not yet built)

> **Audience:** contributors and anyone evaluating J.A.R.N.'s future direction.
> **Nothing described here is shipped.** This document records the intended design
> so the core stays Web-UI-ready; see [ROADMAP.md](ROADMAP.md) for status.

The Web UI is intentionally **not** built yet. Per [SPEC.md](../SPEC.md), J.A.R.N.
ships TUI-first and a Web UI follows only once the core is launch-ready. This
document records the intended design so the core stays Web-UI-ready.

## Why it's deferred

- A web product needs a server, auth, session transport, and a frontend — a large
  surface that would dilute the TUI focus before launch.
- It requires hosting/infra decisions that belong to the open-core/commercial phase
  (see [OPEN_CORE.md](OPEN_CORE.md)).

## How the core is already prepared

The agent core is deliberately UI-agnostic. The same components the TUI uses can back
a web server with no changes to the engine:

```
            ┌──────────────── shared core (no UI deps) ────────────────┐
TUI  ──────▶│ Controller → build_runtime → SessionDriver → Events       │
WebSocket ─▶│ PermissionEngine · CostTracker · Memory · Extensibility   │
            └───────────────────────────────────────────────────────────┘
```

- `SessionDriver.run_turn` is an async generator of typed `Event`s — trivially
  mappable to Server-Sent Events / WebSocket frames.
- The `approver` callback is an abstract async function; the web layer would
  implement it by round-tripping an approval prompt to the browser.
- `Controller` owns runtime/threads/commands and has no Textual imports.

## Intended stack (when built)

- **Server:** FastAPI on Fluid Compute (Node 24 / Python 3.13), WebSocket per session.
- **Transport:** stream `Event`s as JSON frames; resume via the same SQLite/threads.
- **Frontend:** Next.js App Router (AI SDK v6) consuming the event stream.
- **Auth:** Sign in with Vercel / Clerk via the marketplace.
- **Isolation:** the sandbox backend (see [PERMISSIONS.md](PERMISSIONS.md)) becomes the
  default for hosted multi-tenant use.

## Scaffold

`web/` contains a non-functional architecture stub (`web/README.md` and
`web/server_sketch.py`) that shows how a server would reuse `SessionDriver`. It has
no runtime dependencies and is not wired into the package — it's a design reference,
not a shipped feature.

---

**Related docs:** [OPEN_CORE.md](OPEN_CORE.md) · [ROADMAP.md](ROADMAP.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [← docs index](README.md)
