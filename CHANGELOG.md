# Changelog

All notable changes to J.A.R.N. are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-06-08

First public **alpha** release on PyPI. Terminal-first coding agent harness on
[DeepAgents](https://github.com/langchain-ai/deepagents) / LangGraph.

### Added

- Terminal REPL (`jarn`) with native scrollback, streaming Markdown, tool log,
  inline approvals, adaptive toolbar, and input queue
- Permission engine (plan / ask / auto-edit / yolo) with danger-guard, interrupt →
  approval flow, and persisted allow rules
- Project trust boundary — untrusted `.jarn/config.yaml` capability keys stripped
  until explicitly trusted (`jarn trust`)
- Multi-provider BYO key routing, fallback chain, live cost/budget tracking
- Skills, custom slash commands, subagents, lifecycle hooks, MCP client
- Long-term memory (global + project) with recall and `/memory` CRUD/search
- Resumable sessions (SQLite checkpointer), `/resume` picker, session titles
- `jarn setup`, `jarn doctor` (with extension diagnostics), `jarn init`, `jarn trust`
- Slash-command completion with descriptions; `/help` registry
- 371 automated tests (including packaging gate); CI: ruff, mypy, pytest, wheel smoke

### Security

- SSRF guards on `web_fetch`, cancellable shell, sandbox fail-closed default
- Async-subagent tool gating and ambient LangGraph key leak detection
- See [SECURITY.md](SECURITY.md) for the threat model and reporting

### Known limitations (alpha)

- Runs on the **host filesystem** by default — not a sandboxed VM
- Live model calls require your own API key; CI does not exercise real LLM traffic
- Windows: use WSL; native Windows terminal is unsupported
- Web UI, hosted sandbox, and other post-launch differentiators are not in this release

[0.1.0]: https://github.com/chayapats/jarn/releases/tag/v0.1.0
