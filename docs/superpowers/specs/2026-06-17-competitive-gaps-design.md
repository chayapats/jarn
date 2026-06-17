# Spec — JARN competitive-gaps round (5 features)

**Date:** 2026-06-17 · **Branch:** `feat/competitive-gaps`
**Source:** gap analysis vs Claude Code / Cursor / Cline / Aider (see memory `jarn-competitive-gaps`).
**Bar:** every behaviour change ships a test; `uv run pytest` green; `ruff` + `mypy` clean. One commit per feature.

Decisions locked with the product owner:
- Prompt caching **auto-on by default** for every model that supports it (cloud + local).
- Plan mode = **full handoff** (present plan → approve → auto-execute).
- `/commit` = **generate message + run commit** (with approval); add `/review` for the current diff.
- Image paste = **macOS first**.

---

## F1 — Prompt caching (cloud + local keep-warm)

**Problem:** `builder.py::build_runtime` calls `create_deep_agent` with no `middleware`; every turn re-sends the system prompt + project context uncached → cost + latency tax on every turn. Verified: zero `cache_control` in `src/jarn`.

**Mechanism differs by provider — one strategy resolver:**
Add `prompt_cache_strategy(provider_type) -> {"middleware","server_auto","ollama_keepalive","lmstudio_ttl","none"}` in `providers/models.py`.

> **Implementation correction:** deepagents 0.6.8 `create_deep_agent` already adds
> `AnthropicPromptCachingMiddleware` *unconditionally* (no-op for non-Anthropic), so
> JARN passing its own is a duplicate that `create_agent` rejects at startup. We do
> **not** add middleware; cloud Anthropic caching is on by default via the engine.
> JARN's actual contribution is the local keep-warm below. (Caught by a real
> `build_runtime` smoke test the mocked unit tests couldn't see.)

| Provider | Strategy | Action |
|---|---|---|
| Anthropic | `middleware` | cache-control added by deepagents itself — JARN does nothing |
| OpenAI/OpenRouter/DeepSeek/Groq/Together/Fireworks/xAI/Google/Mistral | `server_auto` | nothing — provider caches by exact prefix server-side |
| Ollama | `ollama_keepalive` | pass `keep_alive` to `ChatOllama` so model + KV cache stay resident |
| LM Studio / openai_compatible(local) | `lmstudio_ttl` | pass `extra_body={"ttl": <secs>}` to `ChatOpenAI` so LM Studio doesn't unload the model |
| (local with no lever) | `none` | rely on prefix stability only |

**Config (`schema.py` + `defaults.py`):**
- `prompt_cache: "auto" | "off"` (default `"auto"`) — top-level or under a new minimal `ModelsConfig`; follow existing schema convention. `off` ⇒ no middleware, no keep_alive injection.
- `keep_alive: int` seconds (default `1800`) — used by Ollama (`keep_alive`) and LM Studio (`ttl`). `0` ⇒ leave provider default. Lives next to `prompt_cache`.

**Wiring:**
- `builder.py`: build a `middleware` list; when main model provider is Anthropic and `prompt_cache != off`, append `AnthropicPromptCachingMiddleware(...)` (confirm constructor kwargs — ttl/min-messages — at implementation time). Pass `middleware=mw or ()`.
- `models.py::_construct_inner`: when `prompt_cache != off`, inject `keep_alive`/`extra_body.ttl` for Ollama/LM Studio respectively. ModelFactory needs read access to the cache config (already holds `config`).
- **Prefix stability (regression guard):** confirm the per-turn recall (`Controller.enrich_turn_input`) appends to the turn input, NOT the system prompt, and the system prompt is frozen at build (it is — `build_system_prompt` runs once in `build_runtime`, `date_context()` computed once). Add a test asserting this so nobody later prepends volatile content.
- `/cost`: if usage metadata carries `cache_read_input_tokens` / `cache_creation_input_tokens`, surface a "cached" line. Best-effort; skip if absent.

**Tests:** strategy mapping per provider type; Anthropic ⇒ middleware present, `off` ⇒ absent; Ollama build receives `keep_alive`; LM Studio build receives `extra_body.ttl`; system-prompt-prefix stability regression. All mock-level (no live calls).

**Risk:** low–med (confirm middleware constructor signature; ChatOpenAI `extra_body` merge).

---

## F2 — Background processes

**Problem:** all shell is blocking (`local_backend.execute` → `proc.communicate(timeout)`, 120s default); can't run a dev server / test-watcher / long build and keep working.

**Design:** new `agent/background.py`:
- `ProcessManager` — registry of `BackgroundProc(id, command, popen, stdout_path, stderr_path, started_at)`. `start(command) -> id`, `status(id) -> {running, exit_code, tail}`, `kill(id)`, `list()`. Output streamed to temp files under the project `.jarn/bg/` so `status` can return a tail without blocking. Own process group (`start_new_session=True`) like `local_backend`.
- 4 tools (registered in `builder.py`, **local backend only**): `run_in_background(command)`, `check_background(id)`, `kill_background(id)`, `list_background()`.

**Safety:** route through the permission engine + danger-guard exactly like shell — add the 4 tool names to the gating in `permissions_bridge`/`interrupt_map` and run the command string through the danger-guard before spawning. `ask` ⇒ approval; `yolo` ⇒ allowed.

**Scope/limits:**
- Registered only when `execution.backend == local` (host). Under docker/sandbox the tools are absent + a one-line note (prevents escaping the container).
- Background procs are **not** killed on turn cancel (Esc) — the point is to outlive a turn. They ARE terminated on app exit (hook into the existing backend `terminate_all`/shutdown path).
- Config `execution.background: bool` (default `true`).

**REPL:** `/ps` lists background jobs (id · status · command); `/ps kill <id>`.

**Tests:** start `echo hi` → poll status → output captured + exit 0; `sleep 60` → kill → status reflects killed; danger command (`rm -rf /`) blocked before spawn; tools absent under docker backend.

**Risk:** medium (cleanup + gating).

---

## F3 — Image paste (macOS)

**Problem:** paste handler is text-only; images only enter via `@path` → `read_file` (`agent/files.py`). User must save + type the path.

**Design:** `repl.py` keybinding (e.g. a dedicated key; document it) → read clipboard image via the first available helper:
1. `pngpaste` (if on PATH),
2. AppKit `NSPasteboard` via pyobjc (if importable),
3. `osascript` PNG extraction fallback.
Write bytes to `<project>/.jarn/pastes/paste-<n>.png`, insert an `@<relpath>` token into the input buffer. On send, the existing multimodal `read_file` path turns it into an image content block — no new model plumbing.

**Fallback:** no helper / no image on clipboard ⇒ status line "no image in clipboard — save it and use @path". Non-macOS ⇒ same fallback (Linux deferred).

**Tests:** mock the clipboard helper → returns PNG bytes → temp file written + `@` token inserted at cursor; empty clipboard → graceful message; helper-detection prefers pngpaste→AppKit→osascript. Platform-gated/mock so CI passes on Linux.

**Risk:** medium (helper availability) — mitigated by detection + fallback.

---

## F4 — Plan-mode handoff

**Problem:** `plan` mode is only a read-only DENY (`permissions/engine.py`) — no "present plan → approve → execute". User must manually `/mode` up and re-prompt.

**Design:**
- New tool `exit_plan_mode(plan: str)` (registered in `builder.py`). System prompt (`prompts.py`) instructs: in plan mode, research read-only, then call `exit_plan_mode` with the proposed plan.
- The tool is an **interrupt tool** (`interrupt_on`) → REPL shows the plan and an approval picker: **[a] approve → auto-edit · [k] approve → ask · [n] keep planning**.
- On approve: controller escalates the live permission mode (default `auto-edit`; `ask` if chosen) and the agent **continues in the same turn** (the tool returns an "approved, proceed" result). On reject: returns "keep planning" → mode stays `plan`, agent revises.
- Config `plan.exit_mode: "auto-edit" | "ask"` (default `auto-edit`) for the default landing mode.

**Untrusted clamp:** an untrusted project is floored at `plan` — `exit_plan_mode` must NOT escalate past the trust floor (respect the existing clamp at the launch boundary / `controller`). If clamped, approving explains trust is required.

**Tests:** plan mode + `exit_plan_mode` ⇒ interrupt raised; approve ⇒ live mode becomes auto-edit/ask; reject ⇒ stays plan; untrusted ⇒ cannot escalate past floor.

**Risk:** medium (interrupt + engine integration).

---

## F5 — `/commit` + `/review`

**Problem:** no git/PR helpers at all. No commit-message generation, no diff review command.

**Design (builtin commands, `extensibility/commands.py` + `tui/controller.py`):**
- `/commit`: pre-compute `git diff --staged` (+ unstaged summary) and `git status`; if empty ⇒ "nothing staged/changed to commit". Seed an agent turn: "Write a concise commit message for the diff below following this repo's conventions, show it to me, then run `git commit`." The actual `git commit` goes through normal approval/danger-guard (commit is not danger-blocked; `ask` prompts). Embedding the diff avoids an extra tool round-trip.
- `/review`: pre-compute `git diff` vs HEAD (or base); seed a **read-only** review turn: "Review the following diff for correctness bugs and quality issues; do not edit." No writes needed.
- Non-git repo ⇒ clear message for both.

**Tests:** `/commit` with a staged diff builds a seeded prompt containing the diff; empty index ⇒ message; non-git ⇒ message; `/review` builds a read-only review prompt with the diff.

**Risk:** low.

---

## Config summary (new keys)
- `prompt_cache: auto|off` (default `auto`)
- `keep_alive: int` secs (default `1800`) — Ollama keep_alive / LM Studio ttl
- `execution.background: bool` (default `true`)
- `plan.exit_mode: auto-edit|ask` (default `auto-edit`)

All added with typed defaults in `schema.py`/`defaults.py`, documented in `docs/CONFIGURATION.md`, back-compat (additive only).

## Execution order
F1 (caching) → F5 (commit/review) → F4 (plan) → F2 (background) → F3 (image). Each: test-first, green, ruff+mypy, commit.
