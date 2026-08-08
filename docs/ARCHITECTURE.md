# Architecture

> **Audience:** contributors and anyone who wants to understand how the pieces fit
> together. Read this before diving into the source.

J.A.R.N. is a thin, opinionated harness around the
[DeepAgents](https://github.com/langchain-ai/deepagents) library. DeepAgents (on
LangGraph) provides the agent loop, the filesystem/shell tools, planning, subagents,
summarization, and the human-in-the-loop (HITL) interrupt machinery. J.A.R.N. owns
everything around it: configuration, the permission engine, model routing, cost
tracking, memory, extensibility, and three front-ends: the interactive terminal REPL
(`jarn.repl`), headless one-shot execution (`jarn.headless`), and the optional
Telegram gateway (`jarn.telegram`, shipped in v0.10.0). They converge on the same
controller, turn runner, permission engine, and agent runtime.

```
┌────────────────────── Front-ends / transports ────────────────────────┐
│ Terminal REPL       Headless (`jarn -p`)       Telegram long-poll     │
│ prompt_toolkit      single process             transport daemon       │
└─────────┬──────────────────┬──────────────────────────┬───────────────┘
          │                  │                          │ private NDJSON
          │                  │                 ┌────────▼─────────────┐
          │                  │                 │ per-root worker      │
          └──────────────────┴─────────────────┤ + durable routing    │
                                              └────────┬─────────────┘
                                                       │
                       ┌───────────────────────────────▼──────────────┐
                       │ Controller + shared turn runner              │
                       │ runtime lifecycle · commands · thread state  │
                       └──────────────┬───────────────────┬───────────┘
                                      │ events            │ approvals
                              ┌───────▼────────┐  ┌───────▼──────────┐
                              │ SessionDriver │  │ PermissionEngine │
                              │ stream/resume │─▶│ + danger-guard   │
                              └───────┬────────┘  └──────────────────┘
                                      │ astream / Command(resume)
        ┌─────────────────────────────▼──── build_runtime ─────────────┐
        │ create_deep_agent(model, backend, prompt, subagents, tools)  │
        └───┬─────────┬──────────┬──────────────┬───────────┬─────────┘
            │         │          │              │           │
       Providers   Backend    Memory       Extensibility   Cost
```

## Subsystems

| Package | Responsibility |
|---|---|
| `jarn.config` | Two-tier YAML loading, typed `Config`, secret resolution (`${ENV}` / keychain) |
| `jarn.providers` | Model-ref parsing, `ModelFactory` (→ `init_chat_model` or Codex App Server adapter), per-task routing |
| `jarn.permissions` | `PermissionEngine` (modes + rules + remembered approvals) and the hard `guard` |
| `jarn.cost` | Pricing table, `CostTracker`, budget warn / hard-stop |
| `jarn.memory` | SQLite checkpointer (resumable sessions), markdown long-term memory, `JARN.md` |
| `jarn.extensibility` | Loaders for skills, commands, custom subagents, hooks, MCP |
| `jarn.agent` | `build_runtime` (deepagents assembly), `SessionDriver`, prompts, verify, permission bridge |
| `jarn.agent.turn_runner` | Front-end-neutral turn orchestration used by the REPL, headless path, and gateway worker |
| `jarn.controller` | Shared runtime lifecycle, built-in command operations, thread state, and root ownership |
| `jarn.tui` | Completion, palette/toolbar tokens, input queue, and logo (Textual only for onboarding) |
| `jarn.repl` | Terminal chat UI (prompt_toolkit + Rich) — layout, keys, command dispatch |
| `jarn.repl_renderer` | Turn streaming renderer (`TurnRenderer`) extracted from `repl.py` |
| `jarn.headless` | Headless one-shot entry point (`jarn -p`); fail-closed tool gating, JSON/structured output |
| `jarn.gateway` | Transport-neutral daemon supervision, per-root workers and leases, durable sessions/approvals, private protocol, scheduler |
| `jarn.telegram` | DM-only auth, aiogram long-poll transport, output/approval cards, media staging, poller exclusion, `jarn gateway` CLI |
| `jarn.extensibility.commands` | Typed `BUILTINS` registry — single source for `/help`, completion, docs |
| `jarn.observability` | Local rotating logs, opt-in LangSmith tracing |
| `jarn.onboarding` | First-run wizard |
| `jarn.cli` | `jarn` entry point and subcommands |
| `jarn.doctor_extensions` | Extension diagnostics for `jarn doctor` (skills, commands, shadowing) |
| `jarn.agent.os_sandbox` | OS-level kernel sandbox for the local shell backend (`sandbox-exec` on macOS, `bwrap` on Linux) |
| `jarn.agent.checkpoint` | Auto-checkpoint machinery: snapshot working tree before each turn, `/undo` / `/redo` / `/checkpoints` using private git refs |
| `jarn.agent.repomap` | Ranked, token-budgeted repo map (stdlib `ast` + light regex for JS/TS/Go/Rust); `repo_map` tool + `/map` command |
| `jarn.agent.docker_backend` | Docker container backend (`CancellableDockerSandbox`): every command + file op runs in an isolated container; project root bind-mounted; hardened with in-container cancel, resource limits (`--memory`/`--pids-limit`/`--cpus`), non-root `--user`, and anti-orphan reaper |
| `jarn.config.profiles` | Named policy presets (`trusted-repo`/`review-only`/`sandbox-required`/`ci`/`offline`) via `jarn --preset` or `/preset`; untrusted projects are clamped to a one-way `review-only` floor enforced in `Controller.apply_mode` |
| `jarn.config.settings` | Curated scalar settings allowlist (`SETTINGS`), `ConfigStore` with ruamel round-trip persistence to `~/.jarn/config.yaml`, and `ConfigPanel` state model; exposed via `/config` interactive panel and `/config get\|set` scripting |
| `jarn.memory.wiki` | Markdown wiki knowledge base (`wiki_search`, `wiki_read`, `wiki_write`, `wiki_append` tools + `/wiki` command) |
| `jarn.compat` | Cross-agent interop: `AGENTS.md` / `CLAUDE.md` context-file discovery and `.claude/` skill/command dirs |

## The turn lifecycle

1. The user submits text in `jarn.repl`. `Controller` routes built-in `/commands`
   locally; otherwise a cancellable asyncio task drives a turn.
2. `Controller.ensure_runtime()` lazily builds the deep agent via `build_runtime`,
   loading MCP tools, skills, subagents, context, and the checkpointer.
3. `SessionDriver.run_turn` calls `agent.astream(...)` with `stream_mode=["messages","updates"]`:
   - **messages** chunks → streamed assistant text + usage recorded to `CostTracker`.
   - **updates** chunks → tool-call notices and, crucially, `__interrupt__` events.

   Each streamed item is normalized by `_unpack_stream_item` into a
   `(namespace, mode, chunk)` triple (the namespace is no longer used for cost).
   `_record_usage(msg)` then attributes the call's cost to the right model:
   `_resolve_model_ref(msg)` reads the model the **provider reports on the message** —
   `response_metadata['model_name']` (OpenAI-compatible, incl. OpenRouter) or `['model']`
   (Anthropic) — and canonicalizes it to one of `known_model_refs` (main model + each
   subagent on its own model + the summarizer) via a bidirectional substring match (so
   `claude-opus-4-8` ↔ `anthropic/claude-opus-4-8` both resolve). `known_model_refs` is
   built by `build_runtime` and threaded `JarnRuntime` → `controller.make_driver` →
   `SessionDriver`. A message with no reported model (e.g. an early streaming chunk) falls
   back to `main_model_ref`; a reported model matching no known ref is recorded under the
   raw provider name (pricing still substring-resolves it). `/compact` records the
   summarizer model's usage the same way.
   After each message the driver re-checks `tracker.should_stop()` and aborts the turn
   cleanly when the budget is exceeded — a pragmatic mid-turn *post-call* check, not
   true pre-invoke per-call enforcement (a follow-up needs a LangChain runnable hook).
4. When a gated tool (`write_file`, `edit_file`, `execute`) is called, DeepAgents'
   HITL middleware **interrupts**. The driver maps the tool call to a permission
   `Action`, asks the `PermissionEngine`, and:
   - `ALLOW` → resume `{"type": "approve"}` automatically,
   - `DENY` → resume `{"type": "reject"}` with a reason,
   - `ASK` → call the UI `approver` (the approval modal) and resume accordingly.
5. The driver resumes the graph with `Command(resume={"decisions": [...]})` and loops
   until there are no more interrupts. For an edit turn with `verify.gate: auto`, it
   then runs the detected acceptance command through the same permission engine. A
   failure is appended to the conversation for up to `verify.max_repair_rounds`
   bounded repair attempts and reverified. Only a passing result emits `DONE`;
   persistent/refused/unavailable verification emits a terminal `ERROR`.

This design keeps **all** authorization logic in J.A.R.N.'s engine; DeepAgents'
interrupts are used purely as the pause/resume mechanism. That's why the danger-guard
can force a confirmation even in YOLO mode.

## System prompt assembly

`build_runtime` keeps a stable, compact reliability prefix and appends context in this
order: local date; the first trusted project context file (`JARN.md`, `AGENTS.md`, or
`CLAUDE.md`); global/project memory indices; auto-skill names and descriptions; and
detected verification commands. Optional wiki and automatic repo-map indices are
volatile suffixes, preserving the stable prefix for provider-side prompt caching.

The base prompt scopes project context and skills to the user's goal and treats source,
web, log, quoted, and tool-result text as data rather than fresh instructions. It also
requires the model to use only tools actually supplied by the active policy/backend.
Plan mode permits local read tools only: write, shell, and network actions remain denied.
Regression tests cap the base at 450 words and 2,900 UTF-8 bytes, below the previous
prompt, while separately pinning its plan/act/verify and instruction-boundary contracts.

## Codex subscription provider

`jarn.providers.codex_subscription` adapts the official Codex App Server's stdio
JSON-RPC agent protocol to LangChain's chat-model interface. Each model invocation
starts an ephemeral Codex thread from the authoritative LangGraph transcript, asks
for a strict `{kind, content, calls}` response, then returns either assistant text or
ordinary LangChain tool calls.

Codex-managed ChatGPT authentication stays outside J.A.R.N.; account status is read
through App Server and an API-key account is explicitly rejected by this provider.
The inner Codex thread has execution, browser/apps, image generation, networking,
and multi-agent capabilities disabled. This prevents an inner agent from bypassing
the existing path:

```text
Codex App Server → strict tool request → LangChain tool call
                 → DeepAgents interrupt → J.A.R.N. PermissionEngine → tool
```

Token updates from App Server are normalized into the same `CostTracker` pipeline.
They retain model attribution but use zero API price because billing/limits belong
to the connected ChatGPT plan.

## Telegram gateway lifecycle

1. `jarn gateway` loads the global config, resolves the bot token, validates the
   deny-by-default user allowlist, and acquires the single-poller host lock.
2. `jarn.telegram.bot` owns `getUpdates`, authenticates every message and callback,
   rejects non-DM traffic, and routes accepted input to `SessionRouter`.
3. `DaemonSupervisor` acquires a per-root lease and starts one worker subprocess for
   each active root. The transport and worker exchange versioned frames over a private
   NDJSON pipe; this protocol is internal, not a public embedding API.
4. The worker rebuilds the same `Controller`/runtime used by local front-ends and runs
   turns through `agent.turn_runner`. Interrupts are parked durably in the root's
   SQLite state while approval cards are routed back to Telegram.
5. A callback verdict resumes the exact parked interrupt. Worker death is reported and
   never auto-replays a turn; idle workers are evicted only when no turn or background
   job is active.

The transport process never owns project execution state. This isolation keeps bot
I/O responsive and prevents one root's process or failure from silently crossing into
another root. Operational details are in [TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md).

## Why this split?

- **Upgradeable core.** DeepAgents is a normal dependency; we track upstream without a
  fork. We read its prebuilt TUI for inspiration but ship our own.
- **Testable seams.** The permission engine, cost tracker, routing, loaders, and the
  `SessionDriver` are all pure-Python and unit-tested without an LLM. The terminal
  front-end is tested headlessly (`test_repl.py`); the onboarding wizard uses Textual's
  pilot. CI gates every push on three checks — `ruff`, `mypy src/` (0 errors), and the
  full `pytest` suite (see CONTRIBUTING.md).
- **Local-first, sandbox-capable, fail-closed.** The default backend is
  `CancellableLocalShellBackend` (a `LocalShellBackend` that runs each command in its own
  process session so Esc/Ctrl+C can kill the whole tree) scoped to the project root.
  `execution.backend: sandbox` switches to the OS-level sandbox (recommended lighter
  default); `execution.backend: docker` switches to `CancellableDockerSandbox` (full
  container isolation). If either can't start, the controller **fails closed** (no
  silent host fallback unless `allow_local_fallback`).
  `Controller.isolation_level()`, the status bar, and `jarn doctor` report the active
  isolation (`docker`/`os-sandbox`/`host`). The seam is `agent/builder.py::_make_backend`.
- **Untrusted projects are gated.** A repo's `.jarn/config.yaml` can't run code or read
  secrets until trusted: `config/trust.py` + `load_config(project_trusted=…)` strip
  capability keys (hooks/MCP/providers/…) until the launcher's trust prompt approves them.
  An untrusted launch also clamps the active policy to the `review-only` floor
  (`jarn.config.profiles`); `/mode`, Shift+Tab, `/sandbox`, and `/preset` cannot loosen
  it until `jarn trust` (or `/trust`) is run.

## Key files

- `agent/builder.py` — the seam between J.A.R.N. and `create_deep_agent`.
- `agent/local_backend.py` — host shell backend with killable process groups.
- `agent/session.py` — streaming + interrupt/approval mediation (`tool_call_id` on events).
- `agent/turn_runner.py` — shared front-end-neutral turn execution and event callbacks.
- `agent/permissions_bridge.py` — tool-name/args → `Action`, and the `interrupt_on` map.
- `permissions/engine.py` + `permissions/guard.py` — the reliability core.
- `config/trust.py` — project trust boundary (capability-key gating).
- `extensibility/commands.py` — typed `BUILTINS` registry (`/help`, completion, README).
- `repl.py` — terminal app (layout, keys, queue drain, command dispatch).
- `repl_renderer.py` — `TurnRenderer` (streaming Markdown, per-tool durations).
- `tui/toolbar.py` — adaptive bottom toolbar; `tui/input_queue.py` — FIFO input queue.
- `tui/palette.py` — theme tokens + `configure_ui(theme, accent)`.
- `agent/os_sandbox.py` — macOS SBPL / Linux bwrap wrappers; path-injection guard.
- `agent/checkpoint.py` — pre-turn snapshots via private git refs; undo/redo stack.
- `agent/repomap.py` — AST + regex source parser; ranked map builder; token budgeting.
- `agent/docker_backend.py` — `CancellableDockerSandbox`; image preflight, resource limits, non-root user, anti-orphan reaper.
- `config/profiles.py` — named policy presets; untrusted `review-only` floor logic.
- `config/settings.py` — `SETTINGS` allowlist, `ConfigStore`, `ConfigPanel`; `/config` panel backend.
- `memory/wiki.py` — wiki page CRUD, slug sanitization, trust-gated project tier.
- `util/atomic.py` — `atomic_write_text` (unique tmp + `os.replace`) and `file_lock` (cross-process, POSIX `flock` / Windows `msvcrt`). Every store that derives new content from the current file holds the lock across load-mutate-publish; the publisher itself never locks, so a caller's lock cannot deadlock against it.
- `headless.py` — single-turn agent runner for `jarn -p`; fail-closed tool gate.
- `controller/` — shared controller state and operations used across front-ends.
- `gateway/daemon.py` + `gateway/worker.py` — per-root process supervision and worker entry.
- `gateway/sessions.py` + `gateway/approvals.py` — durable chat/root/thread routing,
  redacted approval-card persistence, restart re-card, and fail-closed callback ownership.
- `gateway/protocol.py` + `gateway/lease.py` — private NDJSON frames and exclusive root ownership.
- `gateway/scheduler.py` — persistent scheduled jobs with catch-up-once semantics.
- `telegram/cli.py` + `telegram/bot.py` — `jarn gateway`, config validation, auth,
  permanent-401 stand-down, backend/outbox lifecycle, and long-poll transport.
- `telegram/backend.py` — thread-safe worker-event bridge onto the Telegram asyncio
  outbox plus durable approval callback routing.
- `telegram/outbox.py` + `telegram/inbound_media.py` — HTML/draft output, cards, and gated media staging.
- `compat.py` — context-file resolution order and `.claude/` directory discovery.

---

**Related docs:** [CONFIGURATION.md](CONFIGURATION.md) · [PERMISSIONS.md](PERMISSIONS.md) · [EXTENDING.md](EXTENDING.md) · [TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md) · [← docs index](README.md)
