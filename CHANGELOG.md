# Changelog

All notable changes to J.A.R.N. are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-06-09

### Added

- **AGENTS.md / CLAUDE.md interop** — auto-loads the first present of
  `compat.context_files` (default order: `JARN.md`, `AGENTS.md`, `CLAUDE.md`);
  skills and commands are also discovered under `.claude/` dirs
  (`compat.read_claude_dir`, default `true`); `.jarn` always wins on conflict
- **Headless one-shot mode** — `jarn -p "prompt"` (also `--print`) for non-interactive
  use; reads prompt from stdin with `-`; flags `--json`, `--model`,
  `--permission-mode`, `--max-turns`, `--cwd`; fail-closed (gated tools are refused,
  never silently approved, unless an auto-approving mode is set)
- **JSONL session transcript** — append-only log at
  `<project>/.jarn/sessions/<id>.jsonl`; one line per user/assistant/tool event;
  grepping- and git-friendly; configurable via `observability.transcript` (default
  `true`)
- **`!` shell escape** — a REPL line starting with `!` runs a shell command directly
  (no agent, no tokens, no approval prompt)
- **OS-level execution sandbox** — kernel-enforced isolation beneath the danger-guard:
  `sandbox-exec` / SBPL on macOS, `bwrap` on Linux; config keys
  `execution.local_sandbox` (`off` | `auto` | `require`, default `off`),
  `execution.sandbox_allow_network` (default `true`),
  `execution.sandbox_writable` (extra writable paths)
- **Auto-checkpoint + `/undo` / `/redo` / `/checkpoints`** — snapshots the working
  tree before each turn so edits are fully reversible; config `git.autocheckpoint`
  (default `false`), `git.checkpoint_mode` (`shadow` | `commit`, default `shadow`);
  snapshots use private refs and never move HEAD, the branch, or the staged index
- **Repo map** — ranked, token-budgeted codebase overview built with stdlib `ast` +
  light regex for JS/TS/Go/Rust (no extra dependencies); `repo_map` tool +
  `/map [focus] [--refresh]` command; config `context.repo_map` (`off` | `tool` |
  `auto`, default `tool`), `context.repo_map_tokens` (default 1024)
- **Wiki knowledge base** — grep-readable markdown KB at `~/.jarn/wiki` and
  `<project>/.jarn/wiki`; tools `wiki_search` / `wiki_read` (read-only) and
  `wiki_write` / `wiki_append` (gated as WRITE); `/wiki` command; config
  `wiki.enabled` (default `false`); index injected into the prompt; project-tier
  gated by trust

### Security

- `compat` config settings are now actually honored at runtime (previously loaded
  but not applied)
- OS sandbox path-injection guard prevents crafted paths from escaping the sandbox
  root
- `observability` section is now trust-gated — a project config can no longer
  silently enable LangSmith tracing or change the log level
- Untrusted-project wiki pages are not readable by tools (consistent with the
  existing trust gate on project memory and `JARN.md`)
- Transcript tool-argument entries are size-capped to prevent disk exhaustion from
  large tool payloads
- `execution.backend` and `observability.log_level` values are now validated on
  load; invalid values raise `ConfigError` immediately

### Fixed

- `jarn doctor` now reports autocheckpoint, wiki, transcript, and repo_map status
  alongside the existing provider and extension checks

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

[0.2.0]: https://github.com/chayapats/jarn/releases/tag/v0.2.0
[0.1.0]: https://github.com/chayapats/jarn/releases/tag/v0.1.0
