# Roadmap

> **Audience:** users curious about what is shipped versus planned, and contributors
> choosing where to contribute. Items marked `[x]` are implemented and in the
> current release; `[ ]` items are scaffolded or documented but not yet shipped.

Derived from [SPEC.md](../SPEC.md). Status as of **2026-06-09** (v0.3.0 prepared, Alpha; v0.2.0 on PyPI).

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
- [x] `uv`/PyPI packaging, 775 tests (+ packaging gate), clean lint + `mypy` CI
- [x] `jarn doctor` extension diagnostics — skills, commands, subagents, hooks, MCP
  (shadowing, builtin renames, untrusted skips); `uv.lock` tracked for team installs

## v1.x — implemented (since v0.1)

- [x] **Vector recall** over markdown memory — offline `LocalEmbedder` (word +
  char-3gram hashing) + optional provider embeddings; `recall_block` is injected
  per turn via `Controller.enrich_turn_input`; `/memory search/show/add/update/delete`
  spans global + trusted project memory with deduped results and body display
- [x] **Richer `/compact`** — summarize via the summarizer model and continue in a
  fresh thread seeded with the summary
- [x] **Auto-compact** — `should_auto_compact()` compares the context gauge to
  `context.compact_at_pct` and compacts automatically after a turn when over threshold
- [x] **`@file` autocomplete + command palette** — `CompletionProvider` + dropdown
  (Tab to accept) for `/commands` and `@paths`
- [x] **Standalone binary builds** — PyInstaller spec (`packaging/jarn.spec`),
  `scripts/build-binary.sh`, release workflow (built in CI per-OS)
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

## v0.3.0 — prepared (Alpha, unreleased)

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
- [ ] **Sandbox runtimes** beyond the LangSmith provider (Docker / e2b)
- [ ] **Remote telemetry sink** (separately opt-in) on top of the local recorder

## Known limitations

- Live model calls require your own API key; CI tests cover mocked paths only.
- `LocalShellBackend` runs on the host — safety is via the permission engine /
  danger-guard, not isolation (see [PERMISSIONS.md](PERMISSIONS.md)). The sandbox
  backend toggle is wired but needs an external sandbox runtime to function.
- The pricing table is best-effort (override via `~/.jarn/pricing.yaml`).
- `LocalEmbedder` recall is lexical/subword (no neural embeddings) unless a provider
  embedder is configured.

---

**Related docs:** [ARCHITECTURE.md](ARCHITECTURE.md) · [OPEN_CORE.md](OPEN_CORE.md) · [← docs index](README.md)
