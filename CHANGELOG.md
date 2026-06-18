# Changelog

All notable changes to J.A.R.N. are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.4.0] - 2026-06-18

A customer-feedback remediation pass (19 tasks across onboarding, permissions,
approvals, cost/context surfacing, and docs) plus follow-up fixes, then a
competitive-gaps round closing five user pain points versus other harnesses:
prompt caching, plan-mode handoff, `/commit` + `/review`, background processes,
and macOS image paste; then a UX-polish round (16 fixes from an end-to-end
user-journey audit) covering live in-place streaming, onboarding key capture,
in-session auth recovery, faster approvals, cache-aware cost, suggested memory,
rich `@`-mentions, and conversation rewind. A multi-agent review then hardened the
round — fixing a `/rewind` blocker (rewind to the first turn), a cached-token cost
double-count, a per-keystroke `@symbol` stall, and a reasoning-render regression —
and dogfooding against a real LM Studio model surfaced two more: the unpriced-model
notice is now logged (not leaked to the TUI), setup validation shows a spinner +
timeout and is skippable (a cold local model no longer looks like a hang), and token
usage is now tracked for OpenAI-compatible streaming (LM Studio / vLLM). Test count:
789 → 1166.

### Added

- **`/rewind` — branch the conversation to an earlier turn** — pick an earlier
  user turn (arrow-key picker), optionally edit that turn's prompt, and continue
  from there. The rewind *forks* onto a new thread (via the same messages-reducer
  mechanism `/compact` uses), so the original session stays intact and resumable
  in `/resume` — nothing is destroyed. First slice rewinds the **conversation
  only**: file edits made after the chosen turn are **not** reverted; the picker
  and the post-rewind notice point at `/undo` for those. Linking the rewind to the
  git-checkpoint stack so file edits revert atomically is a deferred slice.
- **Live in-place markdown streaming** — assistant output renders as one growing
  *formatted* block in the input region and commits to scrollback once per prose
  run, instead of streaming as dim raw markdown that re-rendered paragraph by
  paragraph. Removes the double-echo flicker, the 8-line preview clip, and literal
  mid-construct markup (open code fences, tables).
- **Core-loop polish** — one-key accept/deny in the approval menu; reasoning text
  streams live during a thinking phase; a steady thinking-word indicator; queued
  input echoes once (not twice); `@file` completion no longer rescans the directory
  on every keystroke; Esc-cancel now states that file edits remain and points at
  rollback (`/abort`).
- **Onboarding that secures a usable key** — the TUI setup wizard now detects an
  existing `*_API_KEY` in your environment, tags a recommended provider, offers a
  model pick-list with a custom-entry fallback for cloud providers, nudges when a
  local endpoint (Ollama / LM Studio) is unreachable, and prompts for/validates a
  key before finishing so the first turn works.
- **In-session auth recovery** — an invalid/expired key now surfaces a friendly
  "key rejected (401) — fix with /key, jarn setup, or your env var" message instead
  of raw SDK JSON; **`/key`** sets or replaces the current provider's key (keychain)
  and rebuilds the runtime without restarting; an auth failure now rotates to a
  configured `routing.fallback` provider instead of dead-ending.
- **Cache-aware cost** — prompt-cache read/write tokens are tracked and shown in
  `/cost` (cloud cache pricing where known); totals still reconcile when a turn has
  no cache usage.
- **Suggested memory** — the agent can propose a memory the user approves
  (`y / N / edit`) before it is written via the existing store (tier + trust gated).
- **Rich `@`-mentions (first slice)** — `@folder` and `@symbol` resolve alongside
  `@path` through an extensible resolver registry; `@url` / `@docs` are deferred.
- **Image paste (macOS)** — **Ctrl+V** grabs a screenshot/image from the clipboard,
  saves it under `.jarn/pastes/`, and inserts it as an `@path` so the agent's
  multimodal `read_file` loads it on send — no more save-to-disk-then-type-the-path.
  Uses `pngpaste` if installed, else an AppleScript fallback; degrades to a hint on
  other platforms or an empty clipboard.
- **Background processes** — `run_in_background` / `check_background` /
  `kill_background` / `list_background` tools let the agent start a dev server,
  watcher, or long build and keep working instead of blocking on output (the
  ordinary `execute` blocks with a 120s timeout). Output streams to a per-process
  log; `/ps` lists them and `/ps kill <id>` stops one. Gated like shell (the
  danger-guard inspects the command); local backend only (`execution.background`,
  default on) — not registered under docker/sandbox; all terminated on exit.
- **Plan-mode handoff** — in read-only `plan` mode the agent now researches, then
  calls a new `exit_plan_mode` tool to present a concrete plan. You approve it
  (arrow-key picker: proceed in auto-edit / proceed asking / keep planning) and the
  session escalates the permission mode and carries the plan out *in the same turn*
  — no manual `/mode` switch and re-prompt. Untrusted projects stay clamped to the
  review-only floor (`/trust` to lift). Default landing mode: `plan.exit_mode`.
- **`/commit` and `/review`** — `/commit` gathers the working-tree diff, has the
  agent draft a conventional commit message, and runs `git commit` (through the
  normal approval path; nothing is pushed). `/review` seeds a read-only review of
  the current diff for correctness bugs and quality. Both embed the diff in the
  seeded turn so the agent skips a tool round-trip.
- **Local prompt-cache keep-warm (`routing.prompt_cache: auto`, default on)** —
  cloud caching is already automatic (the agent engine adds Anthropic cache-control;
  other cloud providers cache by prefix server-side). What was missing was the local
  side: `routing.keep_alive` now keeps an Ollama / LM Studio model + its KV/prefix
  cache resident between turns (Ollama `keep_alive` / LM Studio request `ttl`), so a
  local model doesn't unload on idle and recompute the whole prompt next turn. Cuts
  cost and first-token latency on repeated context.
- **Current-date awareness** — the assembled system prompt states the local
  date/time, so time-sensitive requests ("find today's news") are no longer
  anchored to the model's training cutoff.
- **Context-% gauge for local models** — the toolbar `ctx N%` gauge resolves the
  window for LM Studio (`/api/v0/models`) and Ollama (`/api/show`), so it shows
  for local models, not only curated cloud ones.
- **Live token/throughput while generating** — the spinner/stream footer shows
  the prompt size while processing, then output tokens + a real tok/s rate while
  generating (estimated from the streamed text when the provider streams without
  per-chunk usage, e.g. LM Studio).
- **`/doctor` in the REPL** (same checks as `jarn doctor`, inline); **`/memory
  dump`** (one "what the agent knows" view); **`/abort`** (cancel the turn and
  roll back its edits); **`/preset`** / `--preset` (canonical mode+sandbox
  shortcut).
- **Approvals**: `[v]` view-full-diff through the pager and `[e]`
  edit-before-apply in the menu; **`/compact` preview + confirm**;
  `ui.approval_diff_lines` makes the inline diff cap configurable.
- **Onboarding**: env-key detection + recommended provider + cloud/local/custom
  hints in the wizard; model-slug "did you mean"; local-model discovery (Ollama /
  LM Studio); a one-time unpriced-model warning.
- **Surfacing**: per-tool cost breakdown in `/cost`; web-search source hosts
  inline; grouped `/help` + toolbar glyph legend; always-visible trust indicator;
  `ui.splash: full|compact|off`.

### Changed

- **Unified permission model (P3.A)** — `permission_mode` + `policy.profile`
  collapse into one model: **Mode** (`/mode`), **Sandbox** (`/sandbox`), **Trust**
  (`/trust`), and **Presets** as launch-time shortcuts. `/profile`, `--profile`,
  and `policy.profile` are deprecated aliases of `/preset`/`--preset` (still work,
  with a one-time notice). The untrusted floor is now a direct clamp,
  byte-for-byte equivalent to the old `review-only` floor (pinned by an
  equivalence test). `docs/PERMISSIONS.md` rewritten around the one model.
- Entering **yolo** prints a prominent confirmation banner; `/undo` /`/redo` give
  an actionable message when autocheckpoint is off.

### Fixed

- **Multiple subagent interrupts** — resuming more than one pending HITL interrupt
  is keyed by interrupt id (LangGraph 1.x requirement), fixing the "you must
  specify the interrupt id when resuming" error when several subagents need
  approval at once.
- **Rapid Shift+Tab → yolo** no longer stacks confirmation prompts that fight over
  the input and hang.
- A mid-turn failure now logs the **full traceback** to `~/.jarn/logs/jarn.log`
  instead of showing only a one-line message.

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
