# Contributing

> **Audience:** contributors opening their first PR. Covers dev setup, the CI
> gates you must pass, testing layers, and conventions for common changes.

## Dev setup

```bash
git clone https://github.com/chayapats/jarn && cd jarn
uv sync --extra dev --extra telegram
uv run jarn doctor      # sanity-check config, providers, and extensions
```

Requires Python 3.12+ and uv. macOS / Linux (Windows via WSL). The repo tracks
`uv.lock` — commit changes to it whenever you change dependencies in
`pyproject.toml`. The `telegram` extra installs `aiogram` so gateway/bot
tests match CI (optional at runtime for non-gateway use — omit it if you are
not touching the Telegram transport).

### Team onboarding

Each developer runs `jarn setup` once. Config stores only a keychain,
environment, or private-file reference; it never embeds the API key itself.
When working in a project that declares hooks, MCP servers, or other capability
keys in `.jarn/config.yaml`, either approve the launch-time trust prompt or run
`jarn trust <project-root>` after reviewing the repo. `jarn doctor` lists every
skill, command, subagent, hook, and MCP server that would load, including
shadowed files and project-tier entries skipped on untrusted projects.

## Workflow

```bash
uv run pytest                      # full suite (logic + mocked-agent + terminal REPL)
uv run pytest tests/test_permissions.py -q   # one file
uv run ruff check src tests scripts        # lint
uv run ruff check src tests scripts --fix  # autofix
uv run mypy src/                   # type-check (must report 0 errors)
```

Before pushing, run all three gates locally — `ruff check src tests scripts`, `mypy src/`, and
`pytest` (currently **3375** tests) after `uv sync --extra dev --extra telegram`. CI runs
exactly these on every push/PR (lint → type-check → test, with the telegram extra) across
Linux/macOS/Windows and Python 3.12/3.13, plus a `packaging` job and an `npm` job that runs
the Node launcher + assembly tests (`node --test npm/jarn-cli/test/launcher.test.js` and
`npm/test/build.test.mjs`). The live-LLM end-to-end suite is intentionally **not** part of
that gate (it's slow, costs tokens, and is flaky); run those manually or via the optional
nightly workflow (see `evals/README.md`).

If you touch anything under `npm/` (the `jarn-cli` launcher or the package-assembly
script), run those two `node --test` files locally too.

When adding a built-in command, add a `CommandSpec` in `src/jarn/commands/registry.py`
(not `extensibility/commands.py`). `/help`, usage errors, and Tab completion all
derive from that registry — `tests/test_phase3.py` checks parity. The README
points operators to `/help` rather than duplicating the inventory.

**Doc sync:** user-facing docs live in `README.md`, `README-TH.md`, `JARN.md`, `SPEC.md`, and
`docs/*.md`. Built-in command lists must match `BUILTINS`; test counts must match
`uv run pytest -q`.

## Testing layers

| Layer | Where | Notes |
|---|---|---|
| Unit / logic | `test_config`, `test_permissions`, `test_guard`, `test_cost`, `test_routing`, `test_extensibility`, `test_memory` | pure Python, no LLM, fast |
| Agent integration (mocked) | `test_agent_mocked` | scripted fake agent exercises the SessionDriver + interrupt/approval flow |
| Front-end / UX | `test_repl`, `test_ux`, `test_phase3` | Terminal REPL (headless) + onboarding wizard pilot; registry/toolbar/queue parity |

Highest-value coverage sits on the **permission engine, danger-guard, and the
interrupt→approval flow** — that's the reliability core. Keep it that way.

Optional coverage report (not CI-gated):

```bash
uv run pytest --cov=src/jarn --cov-report=term-missing
```

## Conventions

- **Match surrounding style.** Dataclasses for config/state; small, single-purpose
  modules; module docstrings explaining the *why*.
- **Type everything.** `from __future__ import annotations` at the top of each module.
  `mypy src/` is a hard CI gate (0 errors), so keep new code typed.
- **No surprise side effects.** Functions that touch the clock take the time as an
  argument (e.g. `SessionIndex.touch(..., when=...)`) so they stay deterministic.
- **Fail loud at the boundary, soft in the loop.** Config/secret errors raise; a bad
  MCP server or a panel refresh failure is logged and skipped.
- **The permission engine is the only authorizer.** Don't add ad-hoc allow/deny logic
  elsewhere; route it through `PermissionEngine` / `guard`.

## Adding a provider

1. Add a value to `ProviderType` (`config/schema.py`).
2. Map it in `ModelFactory._construct_inner` (`providers/models.py`) to the right
   `init_chat_model` `model_provider` and kwargs. If it is not a normal LangChain
   SDK, implement a small `BaseChatModel` adapter (for example
   `providers/codex_subscription.py`) and keep its transport/auth boundary explicit.
3. Add defaults to `config/defaults.py` and pricing to `cost/pricing.py`.
4. Cover factory/config/cost behavior and one full mocked DeepAgents tool round trip.
   Protocol adapters should use a fake local server in CI; live account tests remain
   manual so CI never needs personal credentials.

## Adding a built-in command

1. Add a `CommandSpec` to `COMMAND_SPECS` in `src/jarn/commands/registry.py`
   (`layer: core` → controller handler, `layer: ui` → REPL, `layer: both` → either).
   `/help`, completion, usage errors, and README parity tests derive from this registry.
2. Implement the handler in `controller/commands/*.py` (or a REPL mixin) and
   register it in `controller/commands/__init__.py`.
3. Dispatch is automatic: `Controller.handle_command` resolves aliases and
   case-insensitive names from the registry.

---

**Related docs:** [ARCHITECTURE.md](ARCHITECTURE.md) · [EXTENDING.md](EXTENDING.md) · [← docs index](README.md)
