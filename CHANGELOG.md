# Changelog

All notable changes to J.A.R.N. are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.0] - 2026-06-09

Still **alpha** (`Development Status :: 3 - Alpha`). v1.0.0 is not yet earned —
the road to 1.0 still wants broader real-world isolation testing, an MCP HTTP
hardening pass, and a longer-lived eval baseline. This release ships the
M1–M4 work below.

### Added

- **M1 — Docker container execution backend** (`execution.backend: docker`):
  every shell command and filesystem mutation runs inside a Docker container
  whose only window onto the host is a bind-mount of the project root. Resource
  limits (`docker_memory`, `docker_pids`, `docker_cpus`), non-root `docker_user`,
  `--network none` when network is denied, image preflight with a clear
  `SandboxUnavailable` error, and deterministic container teardown on close.
  An OS-level sandbox (`execution.local_sandbox`) remains the recommended default
  where Docker is unavailable.
- **M2 — Policy profiles + untrusted floor** (`policy.profile`, `--profile`,
  `/profile`): named bundles (`trusted-repo`, `review-only`, `sandbox-required`,
  `ci`, `offline`) that set permission mode, OS-sandbox mode, sandbox network,
  and whether web tools are registered. An untrusted project is one-way clamped
  to the `review-only` floor across **every** surface (TUI launch, `/mode`,
  Shift+Tab, the mode picker, `/sandbox`, headless `--permission-mode`).
- **M3 — Smoke-eval harness** (`scripts/eval.py`, `evals/`): discovers fixtures,
  drives one headless agent session per fixture against a throwaway repo copy,
  restores protected test files before scoring (anti-gaming), and detects
  regressions against a baseline. Not run in CI; costs real tokens.
- **M4 — `/mcp status`**: lists configured MCP servers with per-server health and
  last error (`No MCP servers configured.` when none).
- **M4 — `/trust` + untrusted launch notice**: `/trust` persists trust for the
  current project, lifts the review-only floor in-session, and rebuilds; the REPL
  surfaces a one-time scrollback notice when launched on an untrusted project.
- **`/config` — friendly interactive settings panel + scriptable get/set**:
  `/config` opens a **tabbed arrow-key panel** (Claude-Code style). Settings are
  grouped into plain-language categories (**←/→**: Models · Safety · Sandbox ·
  Budget · Behavior · Appearance), each shown with a **human label** (e.g. "Run
  commands in", "Auto-checkpoint") not the raw key; **↑/↓** select within a tab.
  **Enter** toggles a bool (`● On` / `○ Off`), cycles an enum, or edits a
  text/number in place (type · Enter saves · Esc cancels). A detail box explains
  the **selected** setting (its description + how to change it) so the screen
  stays uncluttered. `/config get <key>` / `/config set <key> <value>` remain for
  scripting. Every change coerces + validates the value, **persists it
  to `~/.jarn/config.yaml`** (comment-preserving ruamel round-trip, atomic), rolls
  back on an invalid value, and applies live. A curated scalar allowlist (mode,
  models, profile, execution, budget, context, ui, features) — structured /
  capability sections stay file/wizard-only. Untrusted sessions still clamp to the
  review-only floor even when a permissive mode is persisted.
- **`/config` cross-setting consistency checks.** Saving a setting now validates
  the *combination*, not just the value. Genuine contradictions are refused with
  a plain-language reason — e.g. enabling the OS sandbox
  (`execution.local_sandbox`) while the backend isn't `local` (the only backend
  that honours it) — but only when the edit *introduces* the conflict, so a
  pre-existing hand-edited config never blocks an unrelated change. Harmless
  "this knob has no effect right now" cases (a budget threshold with no budget,
  a compact-% with auto-compact off, a value a policy profile will overwrite at
  launch) are saved with a ⚠ note instead of being blocked.

### Security / hardening

- **Docker in-container cancellation now actually kills the process tree.** The
  exec id is embedded in the shell command's argv (a `: JARN_EXEC_ID=<id> ;`
  no-op prefix) so `pkill -f JARN_EXEC_ID=<id>` reliably matches and kills the
  cancelled exec — replacing the prior env-var marker that `pkill -f` never
  matched. The default image is now non-slim `python:3.12` (ships `procps`/`pkill`).
- **Anti-orphan reaper is session-scoped.** Containers carry a per-session
  `jarn-session=<uuid>` label and a pid-file under `~/.jarn/run/`; the reaper only
  removes containers whose owning process is dead, so a concurrent jarn session in
  a sibling process is never destroyed.
- **Web-tools SSRF guard closes a DNS-rebinding TOCTOU.** `_check_host` now
  resolves DNS exactly once and returns the validated IPs, which are pinned at
  connect-time — eliminating the second lookup an attacker could rebind.
  `web_search` now routes through that same guarded, IP-pinned, manual-redirect
  path (previously it used `httpx` auto-redirects with no SSRF check).
- **Eval checker injection closed.** Fixture `checker` strings are validated
  against an allowlist (`python`/`python3`/`pytest`, no shell metacharacters) and
  run with `shell=False`; the eval agent loads an eval-neutral config (no dev-repo
  context/keys bleed into fixture runs).
- **Transcript secret redaction.** User prompts and assistant replies are scrubbed
  of common secret shapes (vendor key prefixes, `NAME=secret` assignments) before
  being persisted to the on-disk JSONL transcript.
- Trust dialog now labels `policy` and `observability` as gated keys.

### Changed

- Default Docker image: `python:3.12-slim` → `python:3.12` (procps/pkill present).
- `!` shell escape is now visually distinct: the input line renders **red + bold**
  while typing, the echoed command shows a red `!` + `(host shell)` marker, and a
  `⚡ host shell — runs on your machine directly; no agent, no approval` header
  precedes its output — so it's unmistakable that it bypasses the agent.
- Version → 0.3.0; classifier stays `Development Status :: 3 - Alpha`.

### Fixed

- **Large write/edit approvals no longer flood the terminal.** The unified diff
  shown before approving a `write_file` / `edit_file` is capped (40 lines) with
  the remainder collapsed to a `… (+N more lines)` footer, so creating or
  rewriting a big file no longer dumps the whole content into the prompt.

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

[0.3.0]: https://github.com/chayapats/jarn/releases/tag/v0.3.0
[0.2.0]: https://github.com/chayapats/jarn/releases/tag/v0.2.0
[0.1.0]: https://github.com/chayapats/jarn/releases/tag/v0.1.0
