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
trait is **reliability**: it plans before acting, can enforce project verification,
and asks before doing anything risky. With `verify.gate: auto`, a failed acceptance
command triggers one bounded repair attempt and blocks successful completion if it
still fails.

It runs entirely in your terminal (a Web UI is on the roadmap, post-launch). Notable
capabilities: **AGENTS.md / CLAUDE.md interop** (works out-of-the-box beside other
agents), **headless one-shot mode** (`jarn -p "..."`), **JSONL session transcripts**,
**`!` shell escape** (output fed into the next agent turn as context), **OS-level execution sandbox** (macOS `sandbox-exec` / Linux
`bwrap`) and **Docker container backend** (`execution.backend: docker`), **presets**
(`/preset`, `jarn --preset`) that set mode + sandbox at once, with an untrusted floor,
**auto-checkpoint + `/undo` / `/redo`**, **repo map** (`/map`), a **wiki knowledge
base** (`/wiki`), **`/config` settings panel** (interactive tabbed UI, persists to
`~/.jarn/config.yaml`), and per-server **MCP health** (`/mcp status`).

> **Status:** this source line targets v1.0.9 General Availability. Publication is
> controlled by automated gates, protected UAT, and strict evidence; consult the
> GitHub Releases, PyPI, and npm pages for the currently published version. v0.10 adds an optional
> **single-operator Telegram gateway**: long-poll DM control, isolated per-root
> workers, durable approval cards, inbound media, scheduling, and VPS/systemd
> deployment. Earlier releases landed **engine reliability**; **UX parity with Claude Code** (live
> in-place streaming, `/theme`, `@git:`/`@url:` mentions, word-level diffs, ghost
> autosuggest, conversation `/rewind` with file restore); **differentiators**
> (`--add-dir` multi-root, inline images, headless `--output-schema`, labelled subagent
> streaming, pluggable web search, an LSP-lite diagnostics loop, a verified-completion
> badge); and **launch systems** (a reusable GitHub Action + a nightly eval harness). The
> architecture, configuration, permission engine, and terminal REPL are implemented and
> tested; live model calls require your own API key. See [CHANGELOG.md](CHANGELOG.md) and
> [SECURITY.md](SECURITY.md).

> **Security:** J.A.R.N. runs tools on your **host** by default (real filesystem +
> shell). A project's `.jarn/config.yaml` can declare hooks, MCP servers, and
> provider overrides — only trust repositories you would run code from. Untrusted
> projects are gated until you approve (`jarn trust`). Read [SECURITY.md](SECURITY.md)
> before use.

## Why J.A.R.N.?

- **Reliable without prompt micromanagement** — a small outcome-and-safety kernel
  leaves the model free to adapt its workflow to the task. The default
  `verify.gate: suggest` shows the detected acceptance command; opt-in
  `verify.gate: auto` runs it before completion.
  The completion badge — `` ⎿ verified: pytest ✓ 214 passed · 3.2s `` — confirms the
  result. A failure is fed back for a bounded repair round and, if still failing,
  ends the turn/headless run as an error instead of success. A diagnostics
  feedback loop (LSP-lite) then lints/type-checks just the files each turn edited
  (ruff + pyright) and can queue one bounded auto-fix round, so the agent catches
  the type error it just introduced (`verify.diagnostics: auto`).
- **Safe by default** — a multi-layer permission system (coarse modes + fine-grained
  rules) sits in front of every file write and shell command, backed by a hard
  *danger-guard* that always confirms catastrophic actions — even in YOLO mode.
- **Instruction-aware** — project context and skills guide only the user's stated
  goal. Source files, web pages, logs, and tool results are treated as data, so
  embedded instructions cannot override user intent or the harness's permission,
  trust, and sandbox boundaries. The agent uses only tools exposed by the active
  policy and backend.
- **Bring your own model** — 14 providers, including **Codex through your ChatGPT
  subscription**, OpenRouter, Anthropic, OpenAI, Google, Mistral, Groq, DeepSeek,
  Together, Fireworks, xAI, Ollama, LM Studio, and a generic OpenAI-compatible
  endpoint, with per-task routing so subagents can use cheaper models.
- **Labelled subagent streaming** — output from delegated `task` subagents is tagged
  with a dim `┊ <name> ` prefix and collapses to a single `└ <name>: working… (N tool
  calls)` status line (full text in the Ctrl+O pager), so parallel subagents no longer
  interleave anonymously.
- **Cost- & context-aware** — live token/cost tracking (with a per-tool breakdown)
  and a per-session budget that can warn or hard-stop; a context-% gauge and live
  generation throughput (tok/s) that work for local models (LM Studio / Ollama)
  too, not just priced cloud ones.
- **Observable prompt modules** — only the 146-word reliability/safety kernel is
  unconditional. Plan guidance, trusted project context, memory/skill/wiki catalogs,
  repo maps, the date, and explicitly loaded skill bodies are activated when relevant,
  budget-capped, and manageable through an interactive `/modules` picker with short
  explanations. `/modules active` retains the detailed scope, source, token, and
  truncation report.
- **Date-aware** — the current local date is injected once per thread/day, so
  "today"-relative requests don't anchor to the model's training cutoff.
- **Pluggable web search** — `web_search` supports Tavily, Brave Search, and Exa in
  addition to the keyless DuckDuckGo fallback.  Set `search.provider: auto` (default)
  and export `TAVILY_API_KEY` / `BRAVE_API_KEY` / `EXA_API_KEY` — the first one set wins.
- **Extensible** — skills, slash commands, custom subagents, lifecycle hooks, and MCP
  servers, all configured through plain files in `~/.jarn` and `.jarn/`.

## Install

macOS (Apple Silicon) and Linux (x64 / arm64) are supported; on Windows use WSL.

**Recommended — one-command installer:**

```bash
jarn_installer_tmp=$(mktemp "${TMPDIR:-/tmp}/jarn-install.XXXXXX") && trap '[ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"' 0 HUP INT TERM && curl -fsSL 'https://raw.githubusercontent.com/chayapats/jarn/main/install.sh' -o "$jarn_installer_tmp" && sh "$jarn_installer_tmp"; jarn_install_rc=$?; [ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"; trap - 0 HUP INT TERM; if [ "$jarn_install_rc" -eq 0 ] || [ "$jarn_install_rc" -eq 10 ]; then exec "$SHELL" -l; else (exit "$jarn_install_rc"); fi
```

The command first downloads the installer to a private temporary file, so a failed
download is never executed. The installer inventories old `jarn` commands, detects
the OS/CPU/libc, verifies the release checksum, stages and smoke-tests the candidate,
activates it transactionally, and verifies what a fresh shell resolves. If a native
binary cannot run (for example because of older GLIBC), it uses an isolated managed
Python fallback. The final `exec` solves the parent-shell PATH limitation; status
`10` means installation is verified but shell activation was still required, and
status `20` means setup remains incomplete.

The installer can acquire a compatible official standalone Codex dependency during
ChatGPT setup after showing its source, version, purpose, destination, and integrity
checks. No separate Node/npm/Python/Codex preparation is required on the standard
path. See the [five-minute quickstart](docs/QUICKSTART.md).

<details>
<summary><strong>Advanced installation alternatives</strong></summary>

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
uv sync --extra dev --extra telegram
uv run jarn
```

`uv.lock` is tracked in the repo so every teammate gets the same dependency versions.

Package-manager installs retain package-manager ownership; use that manager to
update/remove them unless J.A.R.N. presents and you confirm an explicit migration.

</details>

### Sharing with your team

```bash
git clone <repo-url> && cd jarn
uv sync --extra dev --extra telegram
uv run jarn setup          # once per machine — stores only a keychain/env/file reference in config
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

## Uninstall

Removal is itemized and preserves user data by default:

```bash
jarn uninstall                         # choose categories interactively
jarn uninstall --yes                   # remove only the managed executable
jarn uninstall --sessions --cache      # confirm only those two categories
jarn uninstall --credentials --yes     # explicitly remove J.A.R.N. credentials
```

Categories are executable, exclusively owned dependencies, configuration, sessions,
cache/logs/telemetry, and credentials. Shared Node, Python, uv, and Codex installs
are never removed. Project-local `.jarn/` directories are never touched. See
[Update, rollback, and uninstall](docs/UPDATE_ROLLBACK.md).

## Quick start

**With a ChatGPT/Codex subscription (no OpenAI API key billing):**

```bash
jarn setup                # choose “Continue with ChatGPT”
# Setup offers a verified Codex dependency install when needed, then shows login.
jarn auth status          # verifies dependency, auth mode, plan/workspace
cd your-project && jarn
```

J.A.R.N. talks to the official local Codex App Server; it never reads or stores
ChatGPT OAuth tokens. Codex's own execution surfaces are disabled in this provider,
and requested tools are translated back into ordinary J.A.R.N. tool calls so the
existing permission, danger-guard, checkpoint, and `/undo` paths remain authoritative.
Subscription usage is shown as tokens with `$0` API cost and still consumes the
limits/credits of your ChatGPT plan. For shared CI, use an API-key provider instead.

**With OpenRouter OAuth (separate, advanced credential command):**

```bash
jarn login        # opens browser → authorize → key stored in OS keychain
cd your-project
jarn              # launch the TUI (runs setup if still needed)
```

**Or configure manually:**

```bash
jarn setup        # first-run wizard: pick a provider, store your API key, choose defaults
cd your-project
jarn init         # create a JARN.md project-context file (optional but recommended)
jarn              # launch the TUI
jarn doctor       # diagnose config / providers / keys / extensions at any time
jarn bug          # write a privacy-scanned report; ask before opening GitHub
```

**Shell completions (tab-complete subcommands and flags):**

```bash
# zsh — run once, then restart your shell
jarn completions zsh > ~/.zfunc/_jarn
# add to ~/.zshrc if not already: fpath=(~/.zfunc $fpath) && autoload -Uz compinit && compinit

# bash — run once, then source or restart
jarn completions bash > ~/.bash_completions/jarn.bash
# add to ~/.bashrc: source ~/.bash_completions/jarn.bash

# fish — run once
jarn completions fish > ~/.config/fish/completions/jarn.fish
```

On first launch with no config, J.A.R.N. runs the setup wizard automatically.
Transactional setup accepts an environment reference or an in-memory pasted key;
it deliberately does not run OpenRouter OAuth because that exchange persists a key
before final confirmation. Run `jarn login` separately when OAuth is desired.

### Telegram gateway (optional)

v0.10 adds a single-operator, DM-only gateway for an always-on VPS. The npm binary
includes it; Python installs need the optional extra:

```bash
pip install 'jarn[telegram]'       # omit for the standalone/npm distribution
jarn gateway setup
```

The wizard verifies the token with Telegram, asks you to send `/start` to the bot,
discovers and confirms your numeric user ID, stores the token in the OS keychain
(owner-only file fallback), updates the global config transactionally, and on Linux
offers an owner-scoped systemd service. No token or hand-edited YAML is required.
Use `jarn gateway status` afterward. Advanced repo allowlisting and manual service
deployment remain documented in the [Telegram gateway guide](docs/TELEGRAM_GATEWAY.md);
never put `gateway:` in a project's `.jarn/config.yaml`.

## Non-interactive / scripting

```bash
jarn exec "summarise the open TODOs"        # recommended discoverable form
jarn exec --json "what changed?"            # one machine-readable JSON result
jarn -p "summarise the open TODOs"          # one-shot: print reply and exit
echo "what changed?" | jarn -p -            # read prompt from stdin
jarn -p "do X" --json                        # emit JSON: {result, tokens, cost, turns}
jarn -p "do X" --model anthropic/claude-opus-4-8  # override model for this run
jarn -p "do X" --permission-mode auto-edit  # allow file writes without prompting
jarn -p "do X" --cwd /path/to/project       # set working directory
jarn -p "extract the version" --output-schema schema.json --json  # structured output
```

`jarn exec` and the shorter legacy `jarn -p` spelling use the same execution,
permission, output, and exit-code contract.

**Structured output (`--output-schema`):** pass a JSON Schema file to constrain the
agent's final answer. The parsed object replaces the free-text `result` field in the
`--json` envelope, making CI parsing trivial:

```bash
jarn -p "list changed files as JSON" --output-schema files.schema.json --json \
  | jq '.result.files[]'
```

Exit codes when `--output-schema` is used: `0` success (structured object in `result`);
`9` with `error.kind: "schema"` if the agent fails to produce a conforming response;
`2` with `error.kind: "usage"` if the schema file can't be read or parsed.

**Fail-closed safety:** the default modes (`ask` / `plan`) refuse any tool that
would normally prompt for approval and exit non-zero. Pass `--permission-mode
auto-edit` or `yolo` to allow unattended tool use — the danger-guard still
blocks catastrophic commands in every mode.

## In CI

J.A.R.N. ships a [GitHub Actions composite action](action/action.yml) so you
can run it in any workflow — PR review, issue-fix bots, nightly audits.

```yaml
- uses: chayapats/jarn/action@main
  with:
    prompt: "Review this diff: …"
    preset: "review-only"     # read-only; use 'ci' for write-enabled runs
    max_turns: "5"
    api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

**Outputs:** `result`, `cost_usd`, `turns`.

**Docker note:** the default `ci` preset requires Docker (ubuntu runners have
it). For docker-less runners (macOS/Windows) use `preset: trusted-repo` with
`permission_mode: auto-edit` — see [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md).

Example workflows: [PR review bot](examples/github/pr-review.yml) ·
[Issue-fix bot](examples/github/issue-fix.yml).
Full docs: [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md).

## The interface: native inline

```bash
jarn            # start a session
jarn --resume   # pick a previous session to resume on launch
jarn --add-dir ../shared-lib --add-dir ../sibling-repo  # extra writable roots (repeatable)
```

**Multi-root workspaces (`--add-dir`):** by default the agent's write scope is the
project root. Pass `--add-dir <dir>` (repeatable) to grant scoped write access to a
sibling directory too — useful for monorepo/sibling-repo work. Each dir must exist
and be a directory. The launch flag works for headless runs too (`jarn -p … --add-dir
<dir>`). You can also add one mid-session with `/add-dir <path>` (approval-gated in
`ask`/`plan` modes; refused on an untrusted project). Added roots widen
the **write scope only** — project context (JARN.md) is loaded from the primary root,
and checkpoint/undo (`/undo`, `/rewind`) snapshot the **primary root only**. See
[docs/PERMISSIONS.md](docs/PERMISSIONS.md) and [SECURITY.md](SECURITY.md).

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
- Start a line with **`/`** for a command (see below). **`@`** references a file path or a
  rich mention:
  - **`@<path>`** — file or directory (default; bare `@`).
  - **`@folder:<frag>`** — directories only.
  - **`@symbol:<name>`** — repo-map symbol (function / class).
  - **`@git:status|diff|staged|log`** — on submit, replaced by a fenced block of real
    read-only git output (`--porcelain=v1 -b`, `diff`, `diff --staged`,
    `log --oneline -15`). Fixed argv allowlist, no shell, 5 s timeout. Output is
    secret-redacted before injection.
  - **`@url:<url>`** — rewrites to `fetch <url> with web_fetch and use its content`
    at submit time; no pre-fetch (network stays agent-mediated and SSRF-guarded).
- **↑ / ↓** navigate input history.
- **Tab** accepts the highlighted completion (`/command` or `@file`). Completion uses a
  **two-tier fuzzy engine**: exact-prefix matches appear first (unchanged predictability),
  followed by subsequence fuzzy matches — so `/cmit` finds `/commit` and `@pyprjct`
  finds `pyproject.toml`.
- **Ghost autosuggest:** as you type, the most recent matching history entry appears
  as dim ghost text after the cursor (fish/zsh-style). Press **→ (Right arrow)** or
  **Ctrl+E** at the end of the line to accept the full suggestion. The ghost is hidden
  while the completion dropdown is open; Right arrow navigates normally mid-line.
- **Ctrl+R** opens a **reverse-history picker**: an arrow-key overlay over the 50 most
  recent unique history entries with live type-to-filter. Press **↑/↓** to navigate,
  **Enter** to prefill the input (does not submit), **Esc** to cancel. Works even
  while a turn is running.
- **Shift+Tab** cycles the permission mode (plan → ask → auto-edit → yolo); the new
  mode flashes on the input border and stays in the status bar.
- **Ctrl+O** (or **`/expand`**) opens the last turn's full tool output in the pager.
- **Ctrl+V** pastes an image/screenshot from the clipboard — it's saved under
  `.jarn/pastes/` and inserted as an `@path` the agent reads on send.
  Supported on **macOS** (PNG/TIFF/JPEG), **Linux** (Wayland `wl-paste` or X11
  `xclip`), and **Windows** (PowerShell); images over 10 MB are rejected.
  With `execution.inline_images: auto` (the default), an `@`-mentioned image
  (≤ 5 MB) is sent to the model as a **native image content block** in your message
  — so weak vision models see it directly instead of hoping they call `read_file`.
  Set `inline_images: off` for the old text-only `@path` behaviour. If a provider
  rejects images, JARN retries the turn **text-only** once and stops inlining for
  the rest of the session.
- **Esc Esc** (two Esc presses within 500 ms, idle, empty input) opens the **`/rewind`
  picker** — same chord as Claude Code. The first Esc still clears non-empty input;
  only the second Esc on an already-empty buffer fires the picker. After you pick a
  turn, a second arrow-key confirm offers **Restore files too** (revert the working
  tree to that turn's checkpoint, shown as a `git diff --stat` preview) or
  **Conversation only** (leave files as-is). Restoring is itself reversible with
  `/undo`; the file restore needs `git.autocheckpoint` on (otherwise the picker
  quietly rewinds the conversation only, exactly as before).
- **Esc** cancels the running turn. **Ctrl+C** cancels a turn / clears the input,
  and **twice in a row** exits (Claude Code-style). **Ctrl+Q** also quits.
- **Copy text:** the terminal owns selection — just **drag to select and ⌘C**
  (or your terminal's copy), and scroll with your terminal's native scrollback,
  exactly like Claude Code.
- **Notifications:** when a turn takes longer than `ui.notify_min_secs` (default 10 s),
  jarn emits a terminal **bell** (`\a`). Set `ui.notify: desktop` for a native OS
  notification (macOS / Linux), `both` for bell + desktop, or `off` to silence all
  notifications. Approval prompts always ring regardless of elapsed time.
- **Terminal tab title:** jarn sets the terminal-tab title via OSC 2 to show the current
  state — `jarn — <project>` (idle), `✳ jarn — <project>` (working), `⏸ jarn — <project>`
  (waiting for approval). Set `ui.terminal_title: false` to disable.
- **Live plan checklist:** when the agent plans, a `⏺ Todos` checklist appears above the
  input and updates **in place** as items flip (✔ done / ◐ in progress / ☐ pending),
  Claude Code-style, with the streaming reply below it. A long plan is capped (overflow
  collapses to `… +N more`); the full list is committed to scrollback at turn end.

Assistant replies render as **Markdown** (headings, lists, syntax-highlighted code).

`/model`, `/mode`, and `/sessions` (alias `/resume`) with no argument open an **arrow-key picker**
(↑/↓ + Enter; Esc cancel). `/model` also offers a custom ref prompt. `/sessions [q]` filters the list.

While a turn is running, submitted lines are **queued** (shown in the toolbar as
`queue N`); manage them with `/queue`, `/queue clear`, `/queue cancel <n>`, or
`/queue move <from> <to>`.

**Mid-turn steering.** Don't want to wait for the queued line to run next turn?
Steer it **into** the running turn: press **`[s]`** (steer now) on the freshly
queued line, or run `/queue steer <n>` to promote line _n_. The steer is appended
to the conversation as a new user message and the agent sees it **before its next
tool call** — great for course-correcting a long refactor ("actually, use
`pathlib`") without cancelling and re-prompting. Steering re-runs only the
in-flight model step with your guidance (one extra model call); completed tool
results are never re-run, so it never strands a tool call mid-flight. If the turn happens to finish first, the steer
runs as the next turn (never lost). Disable with `ui.steering: false` (hides the
`[s]` affordance; `/queue steer` then declines politely).

### Built-in commands

| Command | Description |
|---|---|
| `/help [name]` | Show commands, or details for one command. |
| `/status` | Show directory, model, mode, context, and a local recap. |
| `/model [name\|refresh]` | Show or switch the active model. |
| `/mode [plan\|ask\|auto-edit\|yolo]` | Show or switch how much J.A.R.N. may change. |
| `/theme [dark\|light\|high-contrast\|auto]` | Show or switch the color theme. |
| `/cost` | Show session tokens and estimated cost (alias: /usage). |
| `/context [all]` | Show what is filling the context window. |
| `/verbose` | Cycle how much tool activity is shown. |
| `/focus [on\|off\|status]` | Hide tool chrome and show only the answer. |
| `/modules [active]` | Open the prompt-module picker. |
| `/module [on <name> [turn\|session] \| off <name>]` | Activate or deactivate a prompt module. |
| `/undo` | Revert the last agent turn's file changes. |
| `/redo` | Re-apply the last undone file changes. |
| `/abort` | Stop this turn and roll back its file changes. |
| `/commit` | Draft a commit from the current diff (asks first). |
| `/review` | Read-only review of the current diff. |
| `/diff [staged\|all\|session]` | Show a git diff of staged, working-tree, or session files. |
| `/compact [status]` | Summarize and continue in a fresh thread. |
| `/expand` | Show the last tool output in full. |
| `/memory [search\|show\|add\|update\|delete\|dump] …` | List or edit long-term memory. |
| `/clear` | Start a fresh conversation (alias: /new). |
| `/config [get <key> \| set <key> <value>]` | View or edit settings. |
| `/preset [<name>]` | Show or apply a mode+sandbox shortcut. |
| `/sandbox [docker\|on\|off]` | Show or toggle where commands run. |
| `/trust` | Trust this project and lift the read-only floor. |
| `/add-dir <path>` | Add a directory to this session's write scope. |
| `/mcp [status\|refresh\|prompts\|prompt <server> <name>\|resources\|read <server> <uri>]` | MCP server health, prompts, and resources. |
| `/telemetry status` | Show telemetry opt-in and local sink stats. |
| `/skill <name>` | Invoke a skill by name. |
| `/skills` | List available skills. |
| `/init` | Create a JARN.md project context file. |
| `/permissions` | Show permission rules and the allowlist. |
| `/key [<key>]` | Set the API key for the current provider (keychain). |
| `/login` | Sign in to ChatGPT. |
| `/logout` | Sign out of ChatGPT. |
| `/doctor` | Diagnose configuration, providers, and keys. |
| `/tools` | List tools the agent can use this session. |
| `/rewind` | Rewind to an earlier turn (forks a new thread). |
| `/sessions [q]` | Pick a previous session, or list them (alias: /resume). |
| `/title [text]` | Show or set this session's title. |
| `/checkpoints` | List recent auto-checkpoints. |
| `/ps [kill <id>]` | List or kill background processes. |
| `/queue [clear\|cancel <n>\|move <from> <to>\|steer <n>]` | Show or manage queued input lines. |
| `/busy [interrupt\|queue\|steer\|status]` | Set what Enter does while a turn is running. |
| `/map [focus] [--refresh]` | Show a map of this repository. |
| `/wiki [search <q>\|list]` | Search or list wiki pages. |
| `/quit` | Exit J.A.R.N. (alias: /exit). |

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
JARN.md                  project guidance; bounded excerpt loaded, full file on demand
```

API keys are **referenced, never inlined** — `${ENV_VAR}` or `keychain:jarn/<provider>`.
The `codex_subscription` provider is keyless from J.A.R.N.'s perspective: Codex owns
the managed ChatGPT session created by `jarn codex login`.
Project config is gated by a **trust prompt** (see above). See
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full reference.

At startup jarn quietly checks PyPI for a newer release and prints one dim
line under the splash when an upgrade is available (cached 24 h; skipped under
the `offline` preset or when running headless). Disable with
`updates.check: false` in `~/.jarn/config.yaml`.

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
- [Telegram gateway](docs/TELEGRAM_GATEWAY.md) — bot setup, security model, and systemd deployment
- [Permissions](docs/PERMISSIONS.md) — modes, rules, danger-guard, approvals
- [Extending](docs/EXTENDING.md) — skills, commands, subagents, hooks, MCP
- [Contributing](docs/CONTRIBUTING.md) — dev setup, tests, conventions
- [Roadmap](docs/ROADMAP.md) — what's in v1 / v1.x and what's next
- [Web UI](docs/WEB_UI.md) — planned, post-launch design
- [Open-core](docs/OPEN_CORE.md) — licensing & business model
- [SPEC.md](SPEC.md) — the original design specification

## Troubleshooting

### Esc Esc rewind feels slow or doesn't register

Terminals encode many keys as ESC-prefixed byte sequences (e.g. arrow keys start
with `\x1b[`). To tell a lone Esc from the start of a sequence, prompt_toolkit
waits a short time (~100 ms) after seeing `\x1b` before delivering it as a bare
Esc keystroke. This is inherent to how terminals work — not a jarn bug — and means
the Esc-Esc chord has a slight delay on the first press. The 500 ms window is
generous enough that a normal double-tap still registers.

If the chord never fires, check that neither the **terminal** nor **tmux/screen** is
eating the second `\x1b` (some multiplexers bind Esc for their own prefix key).

### Terminal ignores OSC 2 title updates

Some terminal emulators do not support OSC 2 (`\x1b]2;…\x07`) or suppress it by default.
jarn's tab-title feature is silently no-op in those environments — no visible side-effect
occurs. If you see stray escape characters in your output, set `ui.terminal_title: false`
in `~/.jarn/config.yaml` to disable the feature entirely.

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
uv sync --extra dev --extra telegram
uv run pytest                 # 3154 tests: logic + mocked-agent + packaging gate
uv run ruff check src tests scripts   # lint
uv run mypy src/              # type-check (CI-gated)
uv run jarn doctor            # sanity-check your environment (add --json for machine output)
uv run jarn bug --dry-run    # write scanned JSON to ~/.jarn/bug-report.json
```

## License

Apache-2.0. See [LICENSE](LICENSE).

Built on [DeepAgents](https://github.com/langchain-ai/deepagents),
[LangGraph](https://github.com/langchain-ai/langgraph), [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit),
[Rich](https://github.com/Textualize/rich), and
[Textual](https://github.com/Textualize/textual) (onboarding wizard only).
