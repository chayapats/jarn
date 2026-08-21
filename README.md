<div align="center">

# J.A.R.N. — Just A Reliable Nerd

A TUI-first coding agent harness built on [DeepAgents](https://github.com/langchain-ai/deepagents).

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)

**English** · [ภาษาไทย](README-TH.md)

</div>

---

J.A.R.N. is a terminal coding agent in the spirit of Claude Code and Codex CLI,
built as its own harness on DeepAgents. It plans before it acts, asks before it
takes a risk, and can enforce project verification instead of declaring success
on trust. Tools run on your **host** by default — read [SECURITY.md](SECURITY.md)
before use, and treat a project's `.jarn/config.yaml` as untrusted until you
review it.

## Features

- **Verified completion** — `verify.gate: auto` runs the detected acceptance command, allows one bounded repair, then fails the turn if it still fails (default `verify.gate: suggest`).
- **Diagnostics loop** — `verify.diagnostics: auto` lints files the turn edited (ruff + pyright) and can queue one bounded auto-fix round.
- **Permission system** — every file write and shell command goes through `plan` / `ask` / `auto-edit` / `yolo` plus fine-grained allow/deny rules.
- **Trust gate** — project hooks, MCP servers, and provider overrides stay stripped until you run `jarn trust`.
- **Danger-guard** — `rm -rf`, force-push, `git reset --hard`, and out-of-scope writes always confirm or block, including in YOLO.
- **Sandbox** — OS isolation via macOS `sandbox-exec` / Linux `bwrap`, or `execution.backend: docker`.
- **Bring your own model** — 15 providers, including ChatGPT (Codex subscription), OpenCode Go, OpenRouter, Anthropic, OpenAI, Google, Mistral, Groq, DeepSeek, Together, Fireworks, xAI, Ollama, LM Studio, and OpenAI-compatible endpoints.
- **Headless & CI** — `jarn exec` / `jarn -p`, `--json`, `--output-schema`, and a [GitHub Action](action/action.yml).
- **Project context** — loads `JARN.md`, `AGENTS.md`, or `CLAUDE.md` (first present wins) as data, not a policy override.
- **Extensible** — skills, slash commands, subagents, hooks, and MCP from `~/.jarn` and `.jarn/`.
- **Checkpoints** — auto-checkpoint with `/undo`, `/redo`, and `/rewind`.
- **Multi-root** — `--add-dir` (repeatable) adds extra writable roots; context and undo stay on the primary root.

## Install

macOS (Apple Silicon) and Linux (x64 / arm64). On Windows use WSL. Intel Mac: use pip/uv (no npm binary; the curl installer falls back to managed Python).

**Recommended:**

```bash
jarn_installer_tmp=$(mktemp "${TMPDIR:-/tmp}/jarn-install.XXXXXX") && trap '[ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"' 0 HUP INT TERM && curl -fsSL 'https://raw.githubusercontent.com/chayapats/jarn/main/install.sh' -o "$jarn_installer_tmp" && sh "$jarn_installer_tmp"; jarn_install_rc=$?; [ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"; trap - 0 HUP INT TERM; if [ "$jarn_install_rc" -eq 0 ] || [ "$jarn_install_rc" -eq 10 ]; then exec "$SHELL" -l; else (exit "$jarn_install_rc"); fi
```

The line downloads to a temp file first (a failed `curl` is never executed), then verifies, smoke-tests, and activates the release. Status `10` means installed but the parent shell still needs `exec "$SHELL" -l`. See the [five-minute quickstart](docs/QUICKSTART.md) and [supported platforms](docs/SUPPORTED_PLATFORMS.md).

<details>
<summary>npm / pip / uv / source</summary>

**npm** — self-contained binary, no Python required (Linux x64/arm64, macOS Apple Silicon):

```bash
npm install -g jarn-cli     # provides `jarn` (also `jarn-cli`)
```

**pip** — Python 3.12+:

```bash
pip install jarn
```

**uv:**

```bash
uv tool install jarn
```

**From source:**

```bash
git clone https://github.com/chayapats/jarn && cd jarn
uv sync --extra dev --extra telegram
uv run jarn
```

`uv.lock` is tracked so teammates get the same dependency versions. Package-manager installs stay owned by that manager; use it to update or remove them. See [update, rollback, and uninstall](docs/UPDATE_ROLLBACK.md).

</details>

```bash
jarn uninstall                 # itemized; user data kept by default
jarn uninstall --yes           # managed executable only
jarn uninstall --credentials --yes
```

Project-local `.jarn/` directories are never touched. Shared Node, Python, uv, and Codex installs are never removed.

## Quick start

**ChatGPT / Codex subscription** (no OpenAI API-key billing):

```bash
jarn setup                 # choose “Continue with ChatGPT”
jarn auth status           # dependency, auth mode, plan/workspace — no tokens
cd your-project && jarn
```

J.A.R.N. talks to the official local Codex App Server and does not read or store ChatGPT OAuth tokens. Codex execution surfaces are disabled; requested tools become ordinary J.A.R.N. tool calls so permission, danger-guard, checkpoint, and `/undo` stay in charge. Usage shows tokens at `$0` API cost and still consumes your ChatGPT plan. For shared CI, use an API-key provider.

**OpenRouter OAuth:**

```bash
jarn login                 # browser → authorize → key in the OS keychain
cd your-project && jarn
```

Setup does not run OpenRouter OAuth (that would persist a key before you confirm). Use `jarn login` when you want that path.

**Manual:**

```bash
jarn setup                 # provider, key reference, defaults
cd your-project
jarn init                  # optional JARN.md
jarn
jarn doctor                # config, providers, extensions
```

The wizard also offers **OpenCode Go** (first-run API-key shortcut), other cloud providers, and local models (Ollama / LM Studio). On first launch with no config, setup runs automatically. Use `/help` in the TUI for slash commands.

<details>
<summary>Shell completions</summary>

```bash
# zsh — once, then restart the shell
jarn completions zsh > ~/.zfunc/_jarn
# in ~/.zshrc if needed: fpath=(~/.zfunc $fpath) && autoload -Uz compinit && compinit

# bash
jarn completions bash > ~/.bash_completions/jarn.bash
# in ~/.bashrc: source ~/.bash_completions/jarn.bash

# fish
jarn completions fish > ~/.config/fish/completions/jarn.fish
```

</details>

## Usage

**Interactive:**

```bash
jarn
jarn --resume              # pick a previous session
jarn --add-dir ../lib      # extra writable root (repeatable)
```

Type a message and press Enter. `/help` lists commands. Shift+Tab cycles permission mode. The transcript uses the terminal's native scrollback — no alternate screen.

**Headless / scripting:**

```bash
jarn exec "summarise the open TODOs"
jarn exec --json "what changed?"
jarn -p "summarise the open TODOs"                 # same contract as exec
echo "what changed?" | jarn -p -
jarn -p "do X" --json
jarn -p "do X" --model anthropic/claude-opus-4-8
jarn -p "do X" --mode auto-edit
jarn -p "do X" --cwd /path/to/project
jarn -p "extract the version" --output-schema schema.json --json
```

`--mode` (`plan` / `ask` / `auto-edit` / `yolo`) is the public flag; `--permission-mode` is a hidden alias. Default `ask` / `plan` refuse gated tools and exit non-zero — pass `--mode auto-edit` or `yolo` for unattended runs. Danger-guard still applies.

With `--output-schema`, the parsed object replaces free-text `result` in the `--json` envelope. Exit `0` on success; exit `9` with `error.kind: "schema"` if the answer does not conform; exit `2` with `error.kind: "usage"` if the schema file cannot be read.

**CI** — [GitHub Action](action/action.yml):

```yaml
- uses: chayapats/jarn/action@main
  with:
    prompt: "Review this diff: …"
    preset: "review-only"     # read-only; use 'ci' for write-enabled runs
    api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

Outputs: `result`, `cost_usd`, `turns`. The default `ci` preset needs Docker (Ubuntu runners have it). On docker-less runners use `preset: trusted-repo` with `permission_mode: auto-edit`. Examples: [PR review](examples/github/pr-review.yml) · [issue-fix](examples/github/issue-fix.yml). Full docs: [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md).

**Telegram gateway** (optional, single-operator DM). npm/standalone binaries include it; Python installs need `pip install 'jarn[telegram]'`. Then `jarn gateway setup`. Never put `gateway:` in a project's `.jarn/config.yaml`. See [docs/TELEGRAM_GATEWAY.md](docs/TELEGRAM_GATEWAY.md).

## Permission modes

| Mode | File reads | File writes | Shell | Network |
|---|---|---|---|---|
| `plan` | allow | deny | deny | deny |
| `ask` (default) | allow | ask | ask | ask |
| `auto-edit` | allow | allow in-scope | ask | allow *(read-only)* |
| `yolo` | allow | allow | allow | allow |

In `plan`, the agent researches read-only, then presents a plan (`exit_plan_mode`). Approve it and the session escalates (default `auto-edit` via `plan.exit_mode`) and continues in the same turn. Untrusted projects stay clamped to `plan`.

Danger-guard overrides every mode. Esc/Ctrl+C cancels a turn and kills shells it spawned. Details: [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

## Configuration

Two YAML tiers, project overlays global:

```
~/.jarn/config.yaml      providers, key references, defaults, budget
.jarn/config.yaml        MCP, hooks, permission rules (committable)
JARN.md                  project guidance; bounded excerpt, full file on demand
```

API keys are referenced, never inlined — `${ENV_VAR}` or `keychain:jarn/<provider>`. The `codex_subscription` provider is keyless from J.A.R.N.'s side (`jarn auth login`). Project capability keys are gated by trust (`jarn trust`). Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Extending

Drop files into `~/.jarn/{skills,commands,agents}` or `.jarn/{...}`:

- **Skills** (`skills/*.md`) — reusable workflows, auto- or manually triggered
- **Commands** (`commands/*.md`) — custom `/slash` prompt templates
- **Subagents** (`agents/*.md`) — specialists the main loop can delegate to
- **Hooks** (config) — shell on lifecycle events
- **MCP servers** (config) — stdio or HTTP tool servers

See [docs/EXTENDING.md](docs/EXTENDING.md) and [examples/](examples/).

## Documentation

- [Quickstart](docs/QUICKSTART.md) · [Supported platforms](docs/SUPPORTED_PLATFORMS.md)
- [Configuration](docs/CONFIGURATION.md) · [Permissions](docs/PERMISSIONS.md) · [Extending](docs/EXTENDING.md)
- [Telegram gateway](docs/TELEGRAM_GATEWAY.md) · [GitHub Action](docs/GITHUB_ACTION.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md) · [Update, rollback, uninstall](docs/UPDATE_ROLLBACK.md)
- [Architecture](docs/ARCHITECTURE.md) · [Contributing](docs/CONTRIBUTING.md) · [docs index](docs/README.md)
- [SPEC.md](SPEC.md) · [CHANGELOG.md](CHANGELOG.md) · [SECURITY.md](SECURITY.md)

## Development

```bash
uv sync --extra dev --extra telegram
uv run pytest                 # 3375 tests: logic + mocked-agent + packaging gate
uv run ruff check src tests scripts
uv run mypy src/
uv run jarn doctor            # add --json for machine output
```

## License

Apache-2.0. See [LICENSE](LICENSE).

Built on [DeepAgents](https://github.com/langchain-ai/deepagents),
[LangGraph](https://github.com/langchain-ai/langgraph),
[prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit),
[Rich](https://github.com/Textualize/rich), and
[Textual](https://github.com/Textualize/textual) (onboarding wizard).
