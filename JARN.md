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
  `onboarding`, `cli`
- Tests: `tests/` (**371** pytest cases); docs: `docs/` + `README.md`; design: `SPEC.md`

## Conventions

- `from __future__ import annotations` in every module; type everything.
- Dataclasses for config/state. Small, single-purpose modules with "why" docstrings.
- All authorization goes through `jarn.permissions` — no ad-hoc allow/deny elsewhere.
- Functions that need the time take it as an argument (determinism).

## How to run / test

```bash
uv sync --extra dev
uv run pytest                    # full suite (371 tests)
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
  not isolation. Don't weaken the guard.
- Keep the reliability core (engine, guard, interrupt→approval flow) well-tested.
- `jarn doctor` reports loaded extensions (skills/commands/subagents/hooks/MCP) via
  `doctor_extensions.py` — useful when onboarding teammates to a repo with `.jarn/`.
