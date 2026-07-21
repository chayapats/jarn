# J.A.R.N.

> Project context for J.A.R.N. Auto-loaded into the agent's system prompt.

## What this project is

J.A.R.N. ("Just A Reliable Nerd") is a terminal-first coding agent harness built on the
DeepAgents library. The main UI is an inline REPL (`jarn.repl`, prompt_toolkit + Rich);
the first-run setup wizard uses Textual. The harness emphasizes reliability:
plan → act → verify, with a strict permission system in front of every mutating action.

## Stack & layout

- Language: Python 3.12+ (managed with `uv`)
- Agent engine: `deepagents` (on LangGraph)
- Terminal UI: `jarn.repl` (prompt_toolkit + Rich) + `jarn.repl_renderer` (turn streaming)
- Shared UI: `jarn.tui` — `Controller`, palette/toolbar tokens, `InputQueue`, completion
- Commands: typed `BUILTINS` registry in `jarn.extensibility.commands` (single source for
  `/help`, Tab completion, README)
- Onboarding: Textual wizard (`jarn.onboarding`) with Rich fallback
- Source: `src/jarn/` — subsystems: `config`, `providers`, `permissions`, `cost`,
  `memory`, `extensibility`, `agent`, `repl`, `repl_renderer`, `tui`, `observability`,
  `onboarding`, `cli`, `agent/os_sandbox`, `agent/checkpoint`, `agent/repomap`,
  `agent/docker_backend`, `config/profiles`, `config/settings`, `memory/wiki`, `headless`
- Tests: `tests/` (**1938** pytest cases) + `npm/` Node tests (launcher + assembly); docs: `docs/` + `README.md`; design: `SPEC.md`
- Distribution: PyPI (`jarn`) + npm (`jarn-cli`, standalone binary); `npm/` holds the launcher + per-platform packaging, published by the release workflow's `npm` job

## Conventions

- `from __future__ import annotations` in every module; type everything.
- Dataclasses for config/state. Small, single-purpose modules with "why" docstrings.
- All authorization goes through `jarn.permissions` — no ad-hoc allow/deny elsewhere.
- Functions that need the time take it as an argument (determinism).

## How to run / test

```bash
uv sync --extra dev
uv run pytest                    # full suite (1938 tests)
uv run ruff check src tests      # lint
uv run mypy src/                 # type-check (CI-gated)
uv run jarn                      # launch the terminal REPL
uv run jarn setup                # first-run wizard (Textual or Rich fallback)
uv run jarn doctor               # diagnose config/providers
```

## Things the agent should know

- Chat UI lives in `src/jarn/repl.py` (layout, keys, dispatch); turn rendering in
  `repl_renderer.py`; adaptive toolbar in `tui/toolbar.py`. No full-screen Textual chat.
- Built-in slash commands are declared in `extensibility/commands.py` (`BUILTINS`).
  Add new ones there + `Controller._cmd_*` or REPL-native handler — keep `README.md` in sync
  (`tests/test_phase3.py::test_readme_commands_match_registry`).
- Toolbar shows model · mode · queue · ctx · cost; `/queue` manages lines submitted while busy.
- Theme: `palette.configure_ui(theme, accent)`; `NO_COLOR=1` for plain toolbar labels.
- Front-end tests: `test_repl.py`, `test_ux.py`, `test_phase3.py` (registry/queue/toolbar).
- `LocalShellBackend` runs on the host; safety is the permission engine + danger-guard,
  not isolation. Don't weaken the guard. An optional OS-level sandbox layer is in
  `agent/os_sandbox.py` (macOS `sandbox-exec`, Linux `bwrap`); controlled by
  `execution.local_sandbox`. `execution.backend: docker` activates
  `agent/docker_backend.py` (`CancellableDockerSandbox`) for full container isolation;
  `Controller.isolation_level()` + the status bar + `jarn doctor` report
  `docker`/`os-sandbox`/`host`.
- Policy profiles live in `config/profiles.py` — named presets (`trusted-repo`,
  `review-only`, `sandbox-required`, `ci`, `offline`) via `policy.profile`,
  `jarn --profile`, or `/profile`. An untrusted project is clamped to the `review-only`
  floor (enforced in `Controller.apply_mode`); `/mode`, Shift+Tab, `/sandbox`, `/profile`
  cannot loosen it until `jarn trust` / `/trust` is run.
- The `/config` settings panel lives in `config/settings.py` (`SETTINGS` allowlist,
  `ConfigStore`, `ConfigPanel`). `/config` opens an interactive tabbed UI; `/config get|set`
  works for scripting. Settings persist to `~/.jarn/config.yaml` with validation + rollback.
- Auto-checkpoint lives in `agent/checkpoint.py`; `/undo`, `/redo`, `/checkpoints` commands
  use private git refs and never move HEAD. Controlled by `git.autocheckpoint`.
- Repo map is in `agent/repomap.py` (stdlib `ast` + regex; no extra deps); exposed as the
  `repo_map` tool and `/map` command. Controlled by `context.repo_map`.
- Wiki knowledge base is in `memory/wiki.py`; four tools (`wiki_search`, `wiki_read`,
  `wiki_write`, `wiki_append`) + `/wiki` command. Controlled by `wiki.enabled`.
- Headless one-shot entry point is `headless.py`; invoked by `jarn -p "..."`.
- AGENTS.md / CLAUDE.md interop lives in `jarn.memory.context` (context-file
  resolution) and `jarn.extensibility` (`.claude/` skill/command discovery);
  controlled by `CompatConfig` (`jarn.config.schema`) via `compat.context_files`
  and `compat.read_claude_dir`.
- Keep the reliability core (engine, guard, interrupt→approval flow) well-tested.
- `jarn doctor` reports loaded extensions (skills/commands/subagents/hooks/MCP),
  autocheckpoint/wiki/transcript/repo_map status, and active isolation level
  (`docker`/`os-sandbox`/`host`) via `doctor_extensions.py` — useful when onboarding
  teammates to a repo with `.jarn/`.
- `/mcp status` shows per-server MCP health and last error at runtime.
- `/trust` trusts the current project root and lifts the `review-only` floor by
  reloading config; an untrusted-launch notice is printed at session start.
