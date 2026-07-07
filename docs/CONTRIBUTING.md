# Contributing

> **Audience:** contributors opening their first PR. Covers dev setup, the CI
> gates you must pass, testing layers, and conventions for common changes.

## Dev setup

```bash
git clone https://github.com/chayapats/jarn && cd jarn
uv sync --extra dev
uv run jarn doctor      # sanity-check config, providers, and extensions
```

Requires Python 3.12+ and uv. macOS / Linux (Windows via WSL). The repo tracks
`uv.lock` — commit changes to it whenever you change dependencies in
`pyproject.toml`.

### Team onboarding

Each developer runs `jarn setup` once (stores their API key under `~/.jarn`).
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
`pytest` (currently **1654** tests). CI runs exactly these on every push/PR
(lint → type-check → test) across Linux/macOS/Windows and Python 3.12/3.13, plus a `packaging`
job and an `npm` job that runs the Node launcher + assembly tests (`node --test
npm/jarn-cli/test/launcher.test.js` and `npm/test/build.test.mjs`). The live-LLM
end-to-end suite is intentionally **not** part of that gate (it's slow, costs tokens,
and is flaky); run those manually or via the optional nightly workflow (see `evals/README.md`).

If you touch anything under `npm/` (the `jarn-cli` launcher or the package-assembly
script), run those two `node --test` files locally too.

When adding a built-in command, update `BUILTINS` in `extensibility/commands.py` and
keep `README.md`'s command table in sync — `tests/test_phase3.py` checks parity.

**Doc sync:** user-facing docs live in `README.md`, `JARN.md`, `SPEC.md`, and
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
   `init_chat_model` `model_provider` and kwargs.
3. Add defaults to `config/defaults.py` and pricing to `cost/pricing.py`.
4. Cover it in `tests/test_routing.py`.

## Adding a built-in command

1. Add a `BuiltinCommand` entry to `BUILTINS` in `extensibility/commands.py`
   (`route: controller` + `_cmd_*` handler, or `route: repl` for REPL-native).
   `/help`, completion, and README parity tests derive from this registry.
2. Implement `Controller._cmd_<name>` (`tui/controller.py`) returning a `CommandResult`.
3. The REPL (`repl.py` / `Controller`) dispatches it automatically.

---

**Related docs:** [ARCHITECTURE.md](ARCHITECTURE.md) · [EXTENDING.md](EXTENDING.md) · [← docs index](README.md)
