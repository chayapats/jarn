# Licensing & open-core model

## Today (v0.1)

- The project is **Apache-2.0** (see [LICENSE](../LICENSE)) and developed in a
  license-clean, "ready to open" state: no bundled secrets, no proprietary deps.
- Telemetry is **off by default** and local-only; there is no hosted service.

## Direction (post-launch)

The intended business model is **open-core**:

| Layer | What | License / availability |
|---|---|---|
| **Core** | the TUI harness, agent engine, permissions, extensibility — everything in this repo | Apache-2.0, free |
| **Premium** (future) | hosted sandbox execution, cloud session sync across machines, team sharing of skills/agents/policies, a managed Web UI | Commercial |

Principles that keep the core trustworthy:

- The core is fully usable standalone forever — premium features are additive, not
  gates on existing functionality.
- Anything that leaves your machine is opt-in and clearly labeled.
- Premium integrations plug in through the same public seams documented in
  [EXTENDING.md](EXTENDING.md) and [ARCHITECTURE.md](ARCHITECTURE.md) (backends,
  subagents, MCP), so there is no hidden private API the core depends on.

These are **plans**, not shipped features. No commercial code, accounts, or hosted
infrastructure exist in this repository.
