# Architecture

> **Audience:** contributors and anyone who wants to understand how the pieces fit
> together. Read this before diving into the source.

J.A.R.N. is a thin, opinionated harness around the
[DeepAgents](https://github.com/langchain-ai/deepagents) library. DeepAgents (on
LangGraph) provides the agent loop, the filesystem/shell tools, planning, subagents,
summarization, and the human-in-the-loop (HITL) interrupt machinery. J.A.R.N. owns
everything around it: configuration, the permission engine, model routing, cost
tracking, memory, the extensibility surfaces, and the terminal front-end (`jarn.repl`).

```
┌──────────────────── Terminal front-end (prompt_toolkit) ──────────────┐
│  repl.py — pinned input, native scrollback, approvals, streaming      │
│  controller.py — built-in commands, runtime lifecycle, thread state   │
└───────────────┬─────────────────────────────────────────┬─────────────┘
                │ events                                    │ approval
        ┌───────▼────────┐                          ┌───────▼─────────┐
        │ SessionDriver  │  streams a turn,          │ PermissionEngine │
        │ (agent/session)│  resolves interrupts ────▶│ + danger-guard   │
        └───────┬────────┘                          └──────────────────┘
                │ astream / Command(resume)
        ┌───────▼─────────────────────── build_runtime (agent/builder) ──┐
        │  create_deep_agent(model, backend, system_prompt, subagents,    │
        │                    interrupt_on, checkpointer, tools)           │
        └───┬─────────┬──────────┬──────────┬───────────┬────────────────┘
            │         │          │          │           │
     ModelFactory  Backend   Memory/    Extensibility  CostTracker
     (providers)  (deepagents) Context  (skills/cmds/   (cost)
                  Local+Shell  (memory)  agents/hooks/mcp)
```

## Subsystems

| Package | Responsibility |
|---|---|
| `jarn.config` | Two-tier YAML loading, typed `Config`, secret resolution (`${ENV}` / keychain) |
| `jarn.providers` | Model-ref parsing, `ModelFactory` (→ `init_chat_model`), per-task routing |
| `jarn.permissions` | `PermissionEngine` (modes + rules + remembered approvals) and the hard `guard` |
| `jarn.cost` | Pricing table, `CostTracker`, budget warn / hard-stop |
| `jarn.memory` | SQLite checkpointer (resumable sessions), markdown long-term memory, `JARN.md` |
| `jarn.extensibility` | Loaders for skills, commands, custom subagents, hooks, MCP |
| `jarn.agent` | `build_runtime` (deepagents assembly), `SessionDriver`, prompts, verify, permission bridge |
| `jarn.tui` | Shared controller, completion, palette/toolbar tokens, input queue, logo (Textual only for onboarding) |
| `jarn.repl` | Terminal chat UI (prompt_toolkit + Rich) — layout, keys, command dispatch |
| `jarn.repl_renderer` | Turn streaming renderer (`TurnRenderer`) extracted from `repl.py` |
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
| `jarn.headless` | Headless one-shot entry point (`jarn -p`); fail-closed tool gating, `--json` output, stdin support |
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
   until there are no more interrupts, then emits `DONE`.

This design keeps **all** authorization logic in J.A.R.N.'s engine; DeepAgents'
interrupts are used purely as the pause/resume mechanism. That's why the danger-guard
can force a confirmation even in YOLO mode.

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
- `headless.py` — single-turn agent runner for `jarn -p`; fail-closed tool gate.
- `compat.py` — context-file resolution order and `.claude/` directory discovery.

---

**Related docs:** [CONFIGURATION.md](CONFIGURATION.md) · [PERMISSIONS.md](PERMISSIONS.md) · [EXTENDING.md](EXTENDING.md) · [← docs index](README.md)
