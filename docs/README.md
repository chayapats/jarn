# J.A.R.N. Documentation

The current source line targets J.A.R.N. v1.0.9 General Availability; protected
release gates control publication, and GitHub Releases, PyPI, and npm are the
authoritative sources for the currently published version. J.A.R.N. is a local-first,
permission-gated terminal agent harness built around
[DeepAgents](https://github.com/langchain-ai/deepagents). It wraps the agent
loop with a configurable permission engine, 14-provider model routing (including
managed ChatGPT subscription auth through Codex), cost
tracking, rich extensibility surfaces (skills, custom commands, subagents, hooks,
MCP), a `prompt_toolkit`-based terminal UI, and an optional single-operator Telegram
gateway. New users should follow the [verified one-command quickstart](QUICKSTART.md);
package-manager/source installs are advanced alternatives.

## Table of contents

| Document | Who it's for | What's inside |
|---|---|---|
| [CONFIGURATION.md](CONFIGURATION.md) | Users | Full YAML config reference: providers, routing, budgets, permissions, hooks, MCP, wiki, compat, secrets |
| [AUTH_STATUS_SCHEMA.md](AUTH_STATUS_SCHEMA.md) | Users · Automation | Stable `jarn auth status --json` schema, states, privacy, and exit codes |
| [PERMISSIONS.md](PERMISSIONS.md) | Users · Contributors | How every file write and shell command is authorized — modes, rules, danger-guard, OS sandbox, project trust boundary |
| [EXTENDING.md](EXTENDING.md) | Users · Contributors | Five extension surfaces (skills, slash commands, subagents, hooks, MCP) with a working quick-start |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Contributors | System diagram, subsystem table, turn lifecycle, design rationale, and key source files |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contributors | Dev setup, CI gates (ruff · mypy · full pytest collection), testing layers, and how-to guides for common changes |
| [ROADMAP.md](ROADMAP.md) | Everyone | what is in the v1.0.9 candidate, what is planned, and known limitations |
| [Hermes-aligned display standard](specs/2026-08-15-hermes-aligned-display-standard.md) | Contributors | Plan to make commands and live output as scannable as Hermes Agent — visual grammar, `/help`, toolbar, quiet tool stream |
| [OPEN_CORE.md](OPEN_CORE.md) | Everyone | Licensing (Apache-2.0) and the intended open-core business model — plans only, nothing commercial is shipped |
| [WEB_UI.md](WEB_UI.md) | Contributors | Design notes for a future Web UI — **not built yet**; included so the core stays Web-UI-ready |
| [TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md) | Operators · Contributors | v0.10 setup, global config, security model, systemd deployment, and second-poller stand-down |
| [TELEGRAM_GATEWAY_PARITY.md](TELEGRAM_GATEWAY_PARITY.md) | Contributors | Shipped v1 acceptance record vs map #26 (Implemented · Deferred) |
| [TELEGRAM_GATEWAY_PLAN.md](TELEGRAM_GATEWAY_PLAN.md) | Contributors | Historical gateway decisions and completed task breakdown (#44) |

## Where to start

- **First-time user:** follow [QUICKSTART.md](QUICKSTART.md), then read [CONFIGURATION.md](CONFIGURATION.md).
- **Want to add skills or hooks:** [EXTENDING.md](EXTENDING.md) has a copy-paste quick-start.
- **Adjusting what the agent is allowed to do:** [PERMISSIONS.md](PERMISSIONS.md).
- **Running the Telegram gateway:** [TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md).
- **Opening a PR:** [CONTRIBUTING.md](CONTRIBUTING.md), then [ARCHITECTURE.md](ARCHITECTURE.md) for context.
- **Curious what's next:** [ROADMAP.md](ROADMAP.md).
