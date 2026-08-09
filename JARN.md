# J.A.R.N.

> High-signal project guidance. This whole file is designed to fit the default
> project-context budget. Put longer, task-specific workflows in skills.

## Project

J.A.R.N. ("Just A Reliable Nerd") is a Python 3.12+ terminal coding-agent harness
built on DeepAgents/LangGraph. Its front ends are an inline prompt_toolkit/Rich REPL,
headless one-shot mode, and a single-operator Telegram gateway. Reliability is enforced
through permissions, verification, checkpoints, diagnostics, and honest agent guidance.

Source lives in `src/jarn/`; tests in `tests/`; npm launcher/standalone packaging in
`npm/`; user docs in `README.md`, `README-TH.md`, and `docs/`; design in `SPEC.md`.

## Invariants

- Type Python code and use `from __future__ import annotations`. Prefer dataclasses,
  small single-purpose modules, deterministic time injection, and "why" docstrings.
- All authorization goes through `jarn.permissions`. Never add an ad-hoc bypass or
  weaken the danger guard. Host execution is not isolation; sandbox and Docker policy
  live in `agent/os_sandbox.py` and `agent/docker_backend.py`.
- Untrusted projects remain clamped to plan/review-only until `jarn trust` or `/trust`.
  Project context, config, hooks, MCP, skills, and memory must respect that boundary.
- The Codex subscription bridge in `providers/codex_subscription.py` must keep inner
  Codex execution/network/apps disabled and route every requested tool through J.A.R.N.
- Telegram is global-only and deny-by-default. Keep `docs/TELEGRAM_GATEWAY.md` and the
  Telegram section of `SECURITY.md` synchronized with gateway changes.
- Preserve user work. Auto-checkpoints use private git refs and never move HEAD.

## Routing

- Runtime/prompt/tool assembly: `agent/runtime.py`, `agent/prompts.py`,
  `agent/builtin_tools.py`, `agent/permissions_bridge.py`.
- Shared controller and turn seam: `controller/`, `agent/turn_runner.py`.
- REPL/rendering/UI: `repl/`, `repl_renderer.py`, `tui/`.
- Config/trust/presets/settings: `config/`; built-in slash commands are declared once
  in `extensibility/commands.py` and routed to controller or REPL handlers.
- Extensions: `extensibility/`; memory/context/wiki: `memory/`; providers: `providers/`.
- Gateway/Telegram: `gateway/`, `telegram/`; headless: `headless.py`.
- Repo map, verification, checkpoints: `agent/repomap.py`, `agent/verify.py`,
  `agent/checkpoint.py`.

## Checks

```bash
uv sync --extra dev --extra telegram
uv run pytest
uv run ruff check src tests scripts
uv run mypy src/
node --test npm/jarn-cli/test/launcher.test.js
node --test npm/test/build.test.mjs
```

Keep README command tables synchronized with the typed `BUILTINS` registry. Update
English/Thai/configuration/security docs when behavior or defaults change. Use
`jarn doctor` to inspect loaded extensions, context budgets, provider health, and the
active isolation level.
