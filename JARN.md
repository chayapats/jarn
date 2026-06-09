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
  `onboarding`, `cli`, `os_sandbox`, `checkpoint`, `repomap`, `memory/wiki`, `headless`
- Tests: `tests/` (**755** pytest cases); docs: `docs/` + `README.md`; design: `SPEC.md`

## Conventions

- `from __future__ import annotations` in every module; type everything.
- Dataclasses for config/state. Small, single-purpose modules with "why" docstrings.
- All authorization goes through `jarn.permissions` — no ad-hoc allow/deny elsewhere.
- Functions that need the time take it as an argument (determinism).

## How to run / test

```bash
uv sync --extra dev
uv run pytest                    # full suite (755 tests)
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
  `os_sandbox.py` (macOS `sandbox-exec`, Linux `bwrap`); controlled by
  `execution.local_sandbox`.
- Auto-checkpoint lives in `checkpoint.py`; `/undo`, `/redo`, `/checkpoints` commands
  use private git refs and never move HEAD. Controlled by `git.autocheckpoint`.
- Repo map is in `repomap.py` (stdlib `ast` + regex; no extra deps); exposed as the
  `repo_map` tool and `/map` command. Controlled by `context.repo_map`.
- Wiki knowledge base is in `memory/wiki.py`; four tools (`wiki_search`, `wiki_read`,
  `wiki_write`, `wiki_append`) + `/wiki` command. Controlled by `wiki.enabled`.
- Headless one-shot entry point is `headless.py`; invoked by `jarn -p "..."`.
- AGENTS.md / CLAUDE.md interop lives in `jarn.memory.context` (context-file
  resolution) and `jarn.extensibility` (`.claude/` skill/command discovery);
  controlled by `CompatConfig` (`jarn.config.schema`) via `compat.context_files`
  and `compat.read_claude_dir`.
- Keep the reliability core (engine, guard, interrupt→approval flow) well-tested.
- `jarn doctor` reports loaded extensions (skills/commands/subagents/hooks/MCP) and
  autocheckpoint/wiki/transcript/repo_map status via `doctor_extensions.py` — useful
  when onboarding teammates to a repo with `.jarn/`.
