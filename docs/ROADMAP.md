# Roadmap

> **Audience:** users curious about what is shipped versus planned, and contributors
> choosing where to contribute. Items marked `[x]` are implemented and in the
> current release; `[ ]` items are scaffolded or documented but not yet shipped.

Derived from [SPEC.md](../SPEC.md). Status as of **2026-07-17** (v0.9.0 released on PyPI + npm, Alpha).

## v1 — implemented

**Core**
- [x] Local-first backend (real filesystem + shell), scoped to the project root
- [x] Multi-file read / write / edit, shell execution, planning (`write_todos`)
- [x] Inline subagents via the `task` tool; parallel subagents supported by the engine
- [x] Plan → act → verify system prompt + project capability detection

**Models & cost**
- [x] Multi-provider BYO key (OpenRouter default), Anthropic / OpenAI / Ollama / LM Studio
- [x] Per-task model routing (main / subagent / summarizer) + fallback list
- [x] Live token & cost tracking with per-model attribution, per-session budget (warn + hard-stop + mid-turn re-check)

**Safety**
- [x] Permission modes (plan / ask / auto-edit / yolo)
- [x] Fine-grained allow/deny rules + remember once/session/always
- [x] Hard danger-guard (BLOCKED / DANGEROUS), overrides modes incl. YOLO
- [x] Interrupt → engine → approval flow wired through DeepAgents HITL

**Memory & sessions**
- [x] SQLite checkpointer for resumable sessions + session index
- [x] Markdown long-term memory (global + project) with MEMORY.md index
- [x] Per-turn memory recall + `/memory` CRUD/search/show across global + project tiers
- [x] `JARN.md` project context + `/init`

**Extensibility**
- [x] Skills (hybrid auto/manual triggers)
- [x] Custom slash commands
- [x] Custom subagents
- [x] Lifecycle hooks (pre/post tool, post-edit, pre-commit, session start/end)
- [x] MCP client (stdio + http) — per-server tool-load isolation + health tracking
- [x] Contributor quick start — [EXTENDING.md § Quick start](EXTENDING.md#quick-start-wire-skill--hook--mcp) + [examples/](../examples/)

**TUI & UX**
- [x] Native scrollback stream (Claude Code-style); inline todos + `/resume` picker
- [x] Streaming output, tool-call log, colored diff view, inline approval modal
- [x] Arrow-key pickers for approval, `/model`, `/mode`, `/resume` (↑/↓ + Enter, Esc cancel)
- [x] Extensions load at launch (`/skills`, custom commands before first turn)
- [x] Session title from first prompt (sticky across turns)
- [x] Multiline input (Enter submit, history, `/` commands, Esc cancel)
- [x] Cyan/teal themes (dark / light / high-contrast) + ASCII splash
- [x] Typed command registry (`BUILTINS`) — `/help`, completion, and docs share one source
- [x] Adaptive toolbar (model · mode · queue · context · cost) with width collapse
- [x] Input queue UX — toolbar count + `/queue` list/clear/cancel/move
- [x] Parallel tool-call correlation via `tool_call_id` per-call durations

**Ops**
- [x] First-run onboarding wizard (env or keychain key storage, validation)
- [x] `jarn` / `setup` / `init` / `doctor` (`--json`) CLI
- [x] Strict config validation (typed bools, numeric ranges, unknown top-level keys rejected)
- [x] Local rotating logs, opt-in LangSmith tracing
- [x] `uv`/PyPI packaging, 1680 tests (+ packaging gate), clean lint + `mypy` CI
- [x] `jarn doctor` extension diagnostics — skills, commands, subagents, hooks, MCP
  (shadowing, builtin renames, untrusted skips); `uv.lock` tracked for team installs

## v1.x — implemented (since v0.1)

- [x] **Vector recall** over markdown memory — offline `LocalEmbedder` (word +
  char-3gram hashing) + optional provider embeddings; `recall_block` is injected
  per turn via `Controller.enrich_turn_input`; `/memory search/show/add/update/delete`
  spans global + trusted project memory with deduped results and body display
- [x] **Richer `/compact`** — summarize via the summarizer model and continue in a
  fresh thread seeded with the summary
- [x] **Auto-compact** — a single in-graph summarization pass on the `routing.summarizer`
  model, triggered at `context.compact_at_pct` (built into the agent graph; disable with
  `context.auto_compact: false`)
- [x] **`@file` autocomplete + command palette** — `CompletionProvider` + dropdown
  (Tab to accept) for `/commands` and `@paths`
- [x] **Standalone binary builds** — PyInstaller spec (`packaging/jarn.spec`),
  `scripts/build-binary.sh`, release workflow (built in CI per-OS: linux-x64,
  linux-arm64, macos-arm64)
- [x] **npm distribution (`jarn-cli`)** — the standalone binary, shipped to npm as
  a launcher package + per-platform binary packages (esbuild/ruff model); `npm
  install -g jarn-cli` works with no Python. Published in lockstep with PyPI from
  the same git tag (`npm/` + the release workflow's `npm` job)
- [x] **Opt-in telemetry** — local-only, anonymized, **default OFF**; emits a numeric-only
  `"turn"` event after each turn via `Controller.record_turn` (no prompt/file content)
- [x] **Provider key validation** surfaced in the TUI status bar (●/✗ glyph)
- [x] **Sandbox backend + per-session toggle** — `execution.backend`, `/sandbox`,
  graceful fallback to local when no sandbox runtime is available
- [x] **Async / remote subagents** — `async_subagents` config → DeepAgents
  `AsyncSubAgent` (Agent Protocol)
- [x] **Multimodal filesystem** — DeepAgents `read_file` auto-detects image/PDF/
  audio/video; binary-aware approval diff; `execution.multimodal` flag
- [x] **Turn-level fallback model-swap** — on a turn that fails before producing
  output, rotate through `routing.fallback` and retry transparently

## v0.4.0 — competitive-gaps + UX-polish round (released 2026-06-18)

Five user pain points closed versus other harnesses (Claude Code / Cursor / Cline /
Aider), then a UX-polish round from an end-to-end user-journey audit and a
multi-agent review. See the design spec under `docs/superpowers/specs/`.

- [x] **Local prompt-cache keep-warm** — cloud caching is already automatic (the
  agent engine adds Anthropic cache-control; other cloud providers cache by prefix
  server-side), so the gap was local: `routing.keep_alive` keeps an Ollama / LM Studio
  model + its KV/prefix cache resident between turns (Ollama `keep_alive` / LM Studio
  request `ttl`). `routing.prompt_cache: auto|off` (default `auto`) gates it.
- [x] **Plan-mode handoff** — `exit_plan_mode` tool: in read-only `plan` mode the agent
  presents a plan, you approve it (auto-edit / ask / keep planning), and the session
  escalates and executes in the same turn (clamped on untrusted repos). `plan.exit_mode`
  (default `auto-edit`).
- [x] **`/commit` + `/review`** — `/commit` drafts a conventional message from the diff
  and runs `git commit` (via approval; no push); `/review` seeds a read-only diff review.
- [x] **Background processes** — `run_in_background` / `check_background` /
  `kill_background` / `list_background` tools + `/ps`; local backend only
  (`execution.background`, default `true`), gated like shell, terminated on exit.
- [x] **Image paste (macOS)** — `Ctrl+V` grabs a clipboard image into `.jarn/pastes/`
  and inserts it as an `@path` the multimodal `read_file` loads (pngpaste / AppleScript).
- [x] **`/key`** — fix a rejected/missing API key for the current provider in-session
  (no quit + edit env/keychain + restart). Prompts for the key, stores it in the OS
  keychain, points config at a `keychain:jarn/<provider>` reference (never an inlined
  secret), and rebuilds the runtime so the next turn uses it.
- [x] **Agent-suggested memory** — `suggest_memory` tool: the agent proposes a durable
  memory (name / description / body / type / scope) and the REPL surfaces a "Save this
  memory?" prompt (save / edit-then-save / decline). On approval it writes through the
  existing memory store, respecting the global vs project tier and the project's trust
  gating (project writes refused on an untrusted repo); declining writes nothing. Gated
  and special-cased by the session driver exactly like `exit_plan_mode`.
- [x] **UX-polish round** — live in-place markdown streaming (no raw-preview
  double-echo / 8-line clip), conversation **`/rewind`** (fork to an earlier turn),
  rich **`@`-mentions** (`@folder` / `@symbol`), one-key approval accept/deny, live
  reasoning render, Esc-cancel edit notice, cache-aware `/cost`, friendlier onboarding
  (validation spinner + timeout + skippable), and token usage tracked for
  OpenAI-compatible streaming (LM Studio / vLLM).

## v0.3.0 — released (Alpha, 2026-06-09)

- [x] **Real isolation (M1)** — Docker container execution backend
  (`execution.backend: docker`, hardened) + OS sandbox recommended for untrusted repos
- [x] **Policy profiles (M2)** — `trusted-repo`/`review-only`/`sandbox-required`/`ci`/`offline`
  via `--profile`/`policy.profile`/`/profile`; untrusted projects clamped to a `review-only` floor
- [x] **Quality floor (M3)** — `scripts/eval.py` smoke-eval harness + fixtures (CI-safe offline logic)
- [x] **Release UX (M4)** — `/mcp status`, in-REPL `/trust` + untrusted-launch notice; security
  audit fixes (Docker cancel/reaper, web_fetch SSRF, eval-checker guard, transcript redaction)
- Stays **Alpha**; v1.0.0 pending the road-to-1.0 hardening (see Known limitations).

## v0.2.0 — released

- [x] **AGENTS.md / CLAUDE.md interop** — auto-loads `JARN.md` → `AGENTS.md` →
  `CLAUDE.md` (first present); skills/commands also discovered under `.claude/` dirs
  (`compat.read_claude_dir`); `.jarn` wins on conflict
- [x] **Headless one-shot** — `jarn -p "prompt"` / stdin (`-`); `--json`, `--model`,
  `--permission-mode`, `--max-turns`, `--cwd`; fail-closed (gated tools refused unless
  an auto-approving mode is set)
- [x] **JSONL session transcript** — append-only `<project>/.jarn/sessions/<id>.jsonl`;
  `observability.transcript` (default `true`); trust-gated for project sessions
- [x] **`!` shell escape** — REPL lines starting with `!` run a shell command directly
  with no agent overhead and no approval
- [x] **OS-level execution sandbox** — `sandbox-exec` / SBPL (macOS) or `bwrap`
  (Linux) beneath the danger-guard; `execution.local_sandbox: off|auto|require`
  (default `off`), `sandbox_allow_network`, `sandbox_writable`
- [x] **Auto-checkpoint + `/undo` / `/redo` / `/checkpoints`** — snapshot working tree
  before each turn using private git refs (never moves HEAD); `git.autocheckpoint`
  (default `false`), `git.checkpoint_mode: shadow|commit` (default `shadow`)
- [x] **`/rewind` — branch to an earlier turn (slices 1–2)** — arrow-key picker over
  earlier user turns; forks onto a new thread keeping the prefix, optionally edits
  the chosen prompt, and continues. The original thread stays in `/resume`.
  **Slice 2 (shipped):** a second arrow-key confirm can also restore the working
  tree to the chosen turn's git checkpoint (`git diff --stat` preview, ⚠ on
  uncheckpointed hand-edits), so conversation and files rewind atomically; the
  restore is itself reversible with `/undo` and needs `git.autocheckpoint` on
  (otherwise the picker rewinds the conversation only, as in slice 1). Deferred:
  in-place/destructive same-thread rewind + free message-editing (slice 3); a
  visual branch tree in `/sessions` (slice 4)
- [x] **Repo map** — ranked token-budgeted codebase overview (stdlib `ast` + regex for
  JS/TS/Go/Rust); `repo_map` tool + `/map` command; `context.repo_map: off|tool|auto`
  (default `tool`), `context.repo_map_tokens` (default 1024)
- [x] **Wiki knowledge base** — `~/.jarn/wiki` + `<project>/.jarn/wiki`; tools
  `wiki_search` / `wiki_read` / `wiki_write` / `wiki_append`; `/wiki` command;
  `wiki.enabled` (default `false`); project tier gated by trust

## v2+ / launch-gated (scaffolded + documented, not shipped)

- [ ] **Web UI** — design + core seams ready ([WEB_UI.md](WEB_UI.md), `web/`); the
  agent core is UI-agnostic so a FastAPI/WebSocket server reuses `SessionDriver`
- [ ] **Open-core** — hosted sandbox, cloud sync, team features
  ([OPEN_CORE.md](OPEN_CORE.md)); no commercial code in this repo
- [ ] **Sandbox runtimes** beyond Docker and the LangSmith remote provider (e.g. e2b)
- [ ] **Remote telemetry sink** (separately opt-in) on top of the local recorder

## Known limitations

- Live model calls require your own API key; CI tests cover mocked paths only.
- **Host execution is the default** — `execution.backend: local` runs tools on your
  machine with your user privileges. Safety is via the permission engine and
  danger-guard, not kernel isolation. **Docker** (`execution.backend: docker`) and
  **OS sandbox** (`execution.local_sandbox: auto|require`) are shipped opt-in
  isolation paths; see [PERMISSIONS.md](PERMISSIONS.md).
- The pricing table is best-effort (override via `~/.jarn/pricing.yaml`; set
  `pricing.network: false` to skip live OpenRouter catalog fetch).
- `LocalEmbedder` recall is lexical/subword (no neural embeddings) unless a
  provider embedder is configured (`ProviderEmbedder` is experimental/unwired).

---

**Related docs:** [ARCHITECTURE.md](ARCHITECTURE.md) · [OPEN_CORE.md](OPEN_CORE.md) · [← docs index](README.md)
