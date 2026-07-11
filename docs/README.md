# J.A.R.N. Documentation

J.A.R.N. (v0.8.0, Alpha) is a local-first, permission-gated terminal agent harness built
around [DeepAgents](https://github.com/langchain-ai/deepagents). It wraps the agent
loop with a configurable permission engine, multi-provider model routing, cost
tracking, rich extensibility surfaces (skills, custom commands, subagents, hooks,
MCP), and a `prompt_toolkit`-based terminal UI. Install with `pip install jarn`
(or `npm install -g jarn-cli` for a standalone binary, no Python), then run
`jarn setup` to get started.

## Table of contents

| Document | Who it's for | What's inside |
|---|---|---|
| [CONFIGURATION.md](CONFIGURATION.md) | Users | Full YAML config reference: providers, routing, budgets, permissions, hooks, MCP, wiki, compat, secrets |
| [PERMISSIONS.md](PERMISSIONS.md) | Users · Contributors | How every file write and shell command is authorized — modes, rules, danger-guard, OS sandbox, project trust boundary |
| [EXTENDING.md](EXTENDING.md) | Users · Contributors | Five extension surfaces (skills, slash commands, subagents, hooks, MCP) with a working quick-start |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Contributors | System diagram, subsystem table, turn lifecycle, design rationale, and key source files |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contributors | Dev setup, CI gates (ruff · mypy · 1680 tests), testing layers, and how-to guides for common changes |
| [ROADMAP.md](ROADMAP.md) | Everyone | What is shipped (v0.8.0), what is planned, and known limitations |
| [OPEN_CORE.md](OPEN_CORE.md) | Everyone | Licensing (Apache-2.0) and the intended open-core business model — plans only, nothing commercial is shipped |
| [WEB_UI.md](WEB_UI.md) | Contributors | Design notes for a future Web UI — **not built yet**; included so the core stays Web-UI-ready |

## Where to start

- **First-time user:** run `jarn setup`, then read [CONFIGURATION.md](CONFIGURATION.md).
- **Want to add skills or hooks:** [EXTENDING.md](EXTENDING.md) has a copy-paste quick-start.
- **Adjusting what the agent is allowed to do:** [PERMISSIONS.md](PERMISSIONS.md).
- **Opening a PR:** [CONTRIBUTING.md](CONTRIBUTING.md), then [ARCHITECTURE.md](ARCHITECTURE.md) for context.
- **Curious what's next:** [ROADMAP.md](ROADMAP.md).
