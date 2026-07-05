<div align="center">

```
     ██╗  █████╗  ██████╗  ███╗   ██╗
     ██║ ██╔══██╗ ██╔══██╗ ████╗  ██║
     ██║ ███████║ ██████╔╝ ██╔██╗ ██║
██   ██║ ██╔══██║ ██╔══██╗ ██║╚██╗██║
╚█████╔╝ ██║  ██║ ██║  ██║ ██║ ╚████║
 ╚════╝  ╚═╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝  ╚═══╝
```

**J.A.R.N. — Just A Reliable Nerd**

A TUI-first coding agent harness built on [DeepAgents](https://github.com/langchain-ai/deepagents).

**English** · [ภาษาไทย](README-TH.md)

</div>

---

J.A.R.N. is a terminal coding agent in the spirit of Claude Code and Codex CLI, but
built as its own opinionated harness on top of the DeepAgents library. Its defining
trait is **reliability**: it plans before acting, verifies its own work, asks before
doing anything risky, and never claims success on a guess.

It runs entirely in your terminal (a Web UI is on the roadmap, post-launch). Notable
capabilities: **AGENTS.md / CLAUDE.md interop** (works out-of-the-box beside other
agents), **headless one-shot mode** (`jarn -p "..."`), **JSONL session transcripts**,
**`!` shell escape**, **OS-level execution sandbox** (macOS `sandbox-exec` / Linux
`bwrap`) and **Docker container backend** (`execution.backend: docker`), **presets**
(`/preset`, `jarn --preset`) that set mode + sandbox at once, with an untrusted floor,
**auto-checkpoint + `/undo` / `/redo`**, **repo map** (`/map`), a **wiki knowledge
base** (`/wiki`), **`/config` settings panel** (interactive tabbed UI, persists to
`~/.jarn/config.yaml`), and per-server **MCP health** (`/mcp status`).

> **Status:** v0.5.0 (Alpha) — on PyPI (`pip install jarn`) and npm (`npm install -g
> jarn-cli` — a standalone binary, no Python). Closes five user pain
> points vs other harnesses (prompt caching, plan-mode handoff, `/commit` +
> `/review`, background processes, macOS image paste) plus a UX-polish round (live
> in-place streaming, conversation `/rewind`, rich `@`-mentions, in-session `/key`,
> faster approvals). The architecture, configuration, permission engine, and
> terminal REPL are implemented and tested; live model calls require your own API
> key. See [CHANGELOG.md](CHANGELOG.md) and [SECURITY.md](SECURITY.md).

> **Security:** J.A.R.N. runs tools on your **host** by default (real filesystem +
> shell). A project's `.jarn/config.yaml` can declare hooks, MCP servers, and
> provider overrides — only trust repositories you would run code from. Untrusted
> projects are gated until you approve (`jarn trust`). Read [SECURITY.md](SECURITY.md)
> before use.

## Why J.A.R.N.?

- **Reliable by design** — plan → act → verify is baked into the system prompt, with
  a self-verification loop that runs your project's build/test/lint before reporting done.
- **Safe by default** — a multi-layer permission system (coarse modes + fine-grained
  rules) sits in front of every file write and shell command, backed by a hard
  *danger-guard* that always confirms catastrophic actions — even in YOLO mode.
- **Bring your own model** — 13 providers (OpenRouter, Anthropic, OpenAI, Google,
  Mistral, Groq, DeepSeek, Together, Fireworks, xAI, Ollama, LM Studio, plus a generic
  OpenAI-compatible endpoint) with per-task routing so subagents can use cheaper models.
- **Cost- & context-aware** — live token/cost tracking (with a per-tool breakdown)
  and a per-session budget that can warn or hard-stop; a context-% gauge and live
  generation throughput (tok/s) that work for local models (LM Studio / Ollama)
  too, not just priced cloud ones.
- **Date-aware** — the current local date/time is injected into the system prompt,
  so "today"-relative requests don't anchor to the model's training cutoff.
- **Extensible** — skills, slash commands, custom subagents, lifecycle hooks, and MCP
  servers, all configured through plain files in `~/.jarn` and `.jarn/`.

## Install

macOS (Apple Silicon) and Linux (x64 / arm64) are supported; on Windows use WSL.

**Via npm** — a self-contained binary, **no Python required**:

```bash
npm install -g jarn-cli     # installs the `jarn` command (also available as `jarn-cli`)
```

Intel macs install via pip/uv instead (no npm binary is published for them).

**Via pip / uv** — requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/):

```bash
pip install jarn            # PyPI (alpha)
# or: uv tool install jarn
```

**From source:**

```bash
git clone https://github.com/chayapats/jarn && cd jarn
uv sync --extra dev
uv run jarn
```

`uv.lock` is tracked in the repo so every teammate gets the same dependency versions.

### Sharing with your team

```bash
git clone <repo-url> && cd jarn
uv sync --extra dev
uv run jarn setup          # once per machine — stores API key in ~/.jarn
cd your-project
jarn doctor                # config, providers, and loaded extensions
jarn                       # trust prompt appears if the project declares hooks/MCP
jarn trust .               # pre-approve a repo you control (optional)
```

If a cloned project ships `.jarn/config.yaml` with hooks, MCP servers, or provider
overrides, J.A.R.N. asks before honouring them. Decline to run safely with those
settings stripped, or run `jarn trust <path>` after reviewing the repo. Use
`jarn doctor` to see which skills, commands, subagents, hooks, and MCP servers
would load (including shadowed or skipped files).

## Quick start

```bash
jarn setup        # first-run wizard: pick a provider, store your API key, choose defaults
cd your-project
jarn init         # create a JARN.md project-context file (optional but recommended)
jarn              # launch the TUI
jarn doctor       # diagnose config / providers / keys / extensions at any time
```

On first launch with no config, J.A.R.N. runs the setup wizard automatically.

## Non-interactive / scripting

```bash
jarn -p "summarise the open TODOs"          # one-shot: print reply and exit
echo "what changed?" | jarn -p -            # read prompt from stdin
jarn -p "do X" --json                        # emit JSON: {result, tokens, cost, turns}
jarn -p "do X" --model anthropic/claude-opus-4-8  # override model for this run
jarn -p "do X" --permission-mode auto-edit  # allow file writes without prompting
jarn -p "do X" --cwd /path/to/project       # set working directory
```

**Fail-closed safety:** the default modes (`ask` / `plan`) refuse any tool that
would normally prompt for approval and exit non-zero. Pass `--permission-mode
auto-edit` or `yolo` to allow unattended tool use — the danger-guard still
blocks catastrophic commands in every mode.

## The interface: native inline

```bash
jarn            # start a session
jarn --resume   # pick a previous session to resume on launch
```

J.A.R.N. renders the conversation straight to your terminal's normal buffer —
no alternate screen. The whole transcript lives in your terminal's **native
scrollback**: one scroll gesture scrolls everything and native selection/copy
works across the entire history, exactly like Claude Code. Assistant replies
stream live and render as Markdown; tool calls, approvals, and a per-turn diff
preview appear inline.

## Using J.A.R.N.

```
┌ toolbar: model · mode · queue · ctx · cost ─────────────────────────────┐
│                                                                            │
│   conversation stream (assistant output, tool calls, approvals)           │
│                                                                            │
├────────────────────────────────────────────────────────────────────────┤
│ › your message…                                                          │
└────────────────────────────────────────────────────────────────────────┘
```

- **Type** a message and press **Enter** to send (**Shift+Enter** / **Ctrl+J** for a newline).
- Start a line with **`/`** for a command (see below). **`@`** references a file path.
- **↑ / ↓** navigate input history.
- **Tab** accepts the highlighted completion (`/command` or `@file`).
- **Shift+Tab** cycles the permission mode (plan → ask → auto-edit → yolo); the new
  mode flashes on the input border and stays in the status bar.
- **Ctrl+O** (or **`/expand`**) opens the last turn's full tool output in the pager.
- **Ctrl+V** pastes an image/screenshot from the clipboard — it's saved under
  `.jarn/pastes/` and inserted as an `@path` the agent reads on send.
  Supported on **macOS** (PNG/TIFF/JPEG), **Linux** (Wayland `wl-paste` or X11
  `xclip`), and **Windows** (PowerShell); images over 10 MB are rejected.
- **Esc** cancels the running turn. **Ctrl+C** cancels a turn / clears the input,
  and **twice in a row** exits (Claude Code-style). **Ctrl+Q** also quits.
- **Copy text:** the terminal owns selection — just **drag to select and ⌘C**
  (or your terminal's copy), and scroll with your terminal's native scrollback,
  exactly like Claude Code.
- **Notifications:** when a turn takes longer than `ui.notify_min_secs` (default 10 s),
  jarn emits a terminal **bell** (`\a`). Set `ui.notify: desktop` for a native OS
  notification (macOS / Linux), `both` for bell + desktop, or `off` to silence all
  notifications. Approval prompts always ring regardless of elapsed time.

Assistant replies render as **Markdown** (headings, lists, syntax-highlighted code).

`/model`, `/mode`, and `/resume` with no argument open an **arrow-key picker**
(↑/↓ + Enter; Esc cancel). `/model` also offers a custom ref prompt.

While a turn is running, submitted lines are **queued** (shown in the toolbar as
`queue N`); manage them with `/queue`, `/queue clear`, `/queue cancel <n>`, or
`/queue move <from> <to>`.

### Built-in commands

| Command | Description |
|---|---|
| `/help` | Show available commands and shortcuts. |
| `/init` | Create a JARN.md project context file. |
| `/config` | View or edit settings: /config, /config get <key>, /config set <key> <value> (persists). |
| `/model [/ref\|refresh]` | Show or switch the active model; /model refresh re-queries local endpoints. |
| `/mode [plan\|ask\|auto-edit\|yolo]` | Show or switch the permission mode (plan/ask/auto-edit/yolo). |
| `/sandbox [on\|off]` | Show or toggle the execution backend (local/sandbox). |
| `/key [<key>]` | Set or replace the API key for the current provider (stored in the keychain). |
| `/preset [<preset-name>]` | Show or apply a preset — a shortcut that sets mode + sandbox at once. |
| `/cost` | Show session token usage and cost. |
| `/compact` | Summarize and compact the conversation context. |
| `/expand` | Open the last turn's full tool output in the pager (same as Ctrl+O). |
| `/clear` | Clear the conversation and start a fresh thread. |
| `/sessions` | List and resume previous sessions. |
| `/resume` | Pick a previous session to resume. |
| `/rewind` | Rewind the conversation to an earlier turn and continue (forks a new thread). |
| `/skills` | List available skills. |
| `/memory [search\|show\|add\|update\|delete\|dump] ...` | List, search, show, add, update, delete, or dump long-term memory. |
| `/permissions` | Show current permission rules and allowlist. |
| `/mcp [status] [--refresh]` | Show configured MCP servers with per-server health and last error. |
| `/trust` | Trust this project root and lift the untrusted review-only floor. |
| `/queue [clear\|cancel <n>\|move <from> <to>]` | Show or manage queued input lines (while a turn is running). |
| `/undo` | Revert the last agent turn's file changes. |
| `/redo` | Re-apply the last undone agent turn's file changes. |
| `/abort` | Cancel the running turn and roll back its file changes. |
| `/commit` | Draft a commit message from the current diff and commit (with approval). |
| `/review` | Review the current working-tree diff for bugs and quality (read-only). |
| `/checkpoints` | List recent auto-checkpoints. |
| `/ps [kill <id>]` | List or kill background processes (from run_in_background). |
| `/quit` | Exit J.A.R.N. |
| `/map [focus] [--refresh]` | Show the ranked repo map (codebase overview). |
| `/wiki [search <q>\|list]` | Search or list wiki knowledge-base pages. |
| `/doctor` | Diagnose configuration, providers, and keys. |
| `/telemetry status` | Show telemetry opt-in status and local sink stats. |

## Permission modes

| Mode | File reads | File writes | Shell | Network |
|---|---|---|---|---|
| `plan` | ✅ | ❌ | ❌ | ❌ |
| `ask` (default) | ✅ | ask | ask | ask |
| `auto-edit` | ✅ | ✅ in-scope | ask | ✅ *(read-only)* |
| `yolo` | ✅ | ✅ | ✅ | ✅ |

In **`plan`** mode the agent researches read-only, then presents a concrete plan
(`exit_plan_mode`). Approve it and J.A.R.N. escalates the mode (default `auto-edit`,
configurable via `plan.exit_mode`; the picker also offers `ask`) and carries the plan
out in the same turn — no manual mode switch. Untrusted projects stay clamped to `plan`.

The **danger-guard** overrides all modes: `rm -rf` (incl. `rm -r -f` / `--recursive
--force`), force-push, `git reset --hard`, `mkfs`, fork bombs, out-of-scope writes, etc.
always require explicit confirmation (or are blocked outright). **Esc/Ctrl+C** cancels a
turn *and* kills any shell it spawned. See [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

**Untrusted repos:** a project's `.jarn/config.yaml` can declare hooks, MCP servers, and
providers — capabilities that can run code or read secrets. J.A.R.N. asks you to **trust
a project** before honoring those keys (once per repo); decline and they're ignored while
the session continues safely.

## Configuration

Two tiers, both YAML, merged together (project overrides global):

```
~/.jarn/config.yaml      global: providers, keys (by reference), defaults, budget
.jarn/config.yaml        per-project: MCP servers, hooks, permission rules (committed)
JARN.md                  per-project context, auto-loaded into the system prompt
```

API keys are **referenced, never inlined** — `${ENV_VAR}` or `keychain:jarn/<provider>`.
Project config is gated by a **trust prompt** (see above). See
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full reference.

## Extending

Drop files into `~/.jarn/{skills,commands,agents}` (global) or `.jarn/{...}` (project):

- **Skills** (`skills/*.md`) — reusable knowledge/workflows, auto- or manually-triggered.
- **Commands** (`commands/*.md`) — custom `/slash` prompt templates.
- **Subagents** (`agents/*.md`) — specialist agents the main loop can delegate to.
- **Hooks** (config) — shell commands run on lifecycle events (lint after edit, test before commit).
- **MCP servers** (config) — connect external tool servers (stdio or HTTP).

See [docs/EXTENDING.md](docs/EXTENDING.md) ([quick start](docs/EXTENDING.md#quick-start-wire-skill--hook--mcp)) and [examples/](examples/).

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — how the subsystems fit together
- [Configuration](docs/CONFIGURATION.md) — every config key explained
- [Permissions](docs/PERMISSIONS.md) — modes, rules, danger-guard, approvals
- [Extending](docs/EXTENDING.md) — skills, commands, subagents, hooks, MCP
- [Contributing](docs/CONTRIBUTING.md) — dev setup, tests, conventions
- [Roadmap](docs/ROADMAP.md) — what's in v1 / v1.x and what's next
- [Web UI](docs/WEB_UI.md) — planned, post-launch design
- [Open-core](docs/OPEN_CORE.md) — licensing & business model
- [SPEC.md](SPEC.md) — the original design specification

## Troubleshooting

### Caps Lock inserts a stray `a` (macOS)

On macOS, when Caps Lock is set to switch input source, some terminal apps that
enable the Kitty keyboard protocol's **report-all-keys** mode can leak a stray `a`
into the input. J.A.R.N. disables those flags for Textual (onboarding wizard,
`jarn keys`) and resets any leftover kitty flags before the main REPL starts
(prompt_toolkit does not enable report-all-keys itself).

- Run `jarn keys` (Textual) or `jarn keys --repl` (prompt_toolkit) to see exactly
  what your terminal sends for each key — share a line with a maintainer if you
  hit an unfiltered quirk.
- Set `JARN_KEEP_KITTY_ALL_KEYS=1` to opt out of the fix if you rely on full
  kitty key reporting (e.g. for a custom key-binding workflow).

## Development

```bash
uv sync --extra dev
uv run pytest                 # 1422 tests: logic + mocked-agent + packaging gate
uv run ruff check src tests scripts   # lint
uv run mypy src/              # type-check (CI-gated)
uv run jarn doctor            # sanity-check your environment (add --json for machine output)
```

## License

Apache-2.0. See [LICENSE](LICENSE).

Built on [DeepAgents](https://github.com/langchain-ai/deepagents),
[LangGraph](https://github.com/langchain-ai/langgraph), [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit),
[Rich](https://github.com/Textualize/rich), and
[Textual](https://github.com/Textualize/textual) (onboarding wizard only).
