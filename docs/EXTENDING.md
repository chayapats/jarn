# Extending J.A.R.N.

> **Audience:** users who want to add skills, custom commands, subagents, hooks,
> or MCP tools to J.A.R.N. No code changes required — everything here is
> file- or config-based.

Five extension surfaces, all file- or config-based, all two-tier (global
`~/.jarn/...` and project `.jarn/...`, with the project tier winning on name
conflicts). Working examples live in [`examples/`](../examples).

```
~/.jarn/                         .jarn/                (committed with the repo)
  skills/    *.md                  skills/    *.md
  commands/  *.md                  commands/  *.md
  agents/    *.md                  agents/    *.md
  config.yaml (hooks, mcp)         config.yaml (hooks, mcp, rules)
```

## Which surface should I use?

| Surface | Use when… |
|---|---|
| **Skills** | You want the agent to *automatically* apply a reusable workflow or constraint (e.g. "always back up before running migrations"). The skill file is injected into the system prompt so the model applies it without being told every time. |
| **Custom commands** | You want a user-invoked slash command (`/explain`, `/deploy`) that sends a fixed prompt template with substituted arguments. The agent only uses it when you call it explicitly — it is never auto-triggered. |
| **Subagents** | You need the main agent to delegate a self-contained task to a specialist (e.g. a test-writer that runs independently). Subagents have their own system prompt, optional model, and optional tool restrictions. |
| **Hooks** | You want shell commands to run automatically on lifecycle events (`post_edit`, `pre_commit`, `session_end`, …) without the agent deciding to run them — e.g. auto-lint after every file edit. |
| **MCP** | You want to expose external tools (APIs, databases, file servers) to the agent via the Model Context Protocol. MCP tools appear alongside built-ins and go through the permission engine. |

### Skills vs. custom commands

The two markdown-file surfaces look similar but serve opposite automation modes:

- **Skills** are *agent-driven*: the agent reads the skill catalog at startup and
  decides when to apply a skill based on the task at hand. Use them for standing
  instructions the agent should always follow ("never delete without a backup",
  "prefer functional style in this repo").
- **Custom commands** are *user-driven*: only run when you type `/command-name
  [args]`. Use them for repeatable prompts you want to fire on demand ("review
  my staged diff", "write a changelog entry for this PR").

If you find yourself typing the same prompt repeatedly → custom command.
If you want the agent to follow a rule without being reminded → skill.

## Quick start: wire skill + hook + MCP

End-to-end path for contributors — copy the shipped examples, launch J.A.R.N., and
verify each extension surface without reading `src/jarn/extensibility/`.

### Prerequisites

- Python 3.12+, [uv](https://docs.astral.sh/uv/), and a configured model API key
  (`jarn setup` or `~/.jarn/config.yaml`).
- For the optional MCP stdio example: Node.js + `npx` (see
  [`examples/mcp-filesystem.snippet.yaml`](../examples/mcp-filesystem.snippet.yaml)).

### 1. Copy the examples

From your project root:

```bash
mkdir -p .jarn/skills .jarn/commands .jarn/agents
cp examples/skills/safe-refactor.md .jarn/skills/
cp examples/commands/explain.md     .jarn/commands/
cp examples/agents/test-writer.md   .jarn/agents/
cp examples/project.config.yaml     .jarn/config.yaml
```

See [`examples/README.md`](../examples/README.md) for what each file does.

### 2. Project trust (project-tier only)

Extensions under **`.jarn/`** in a git repo are treated as project-tier. Until you
trust the project, J.A.R.N. strips project hooks, MCP servers, and project
skills/commands/subagents (global `~/.jarn/` still loads). Run once per repo:

```bash
jarn trust .
```

Details: [CONFIGURATION.md § Project trust](CONFIGURATION.md#project-trust),
[PERMISSIONS.md](PERMISSIONS.md).

### 3. Launch and verify

```bash
uv run jarn
```

| Surface | How to verify |
|---|---|
| **Skill** | `/skills` lists `safe-refactor` (auto-triggered; description is in the system catalog). |
| **Command** | `/explain the auth module` sends the explain template with args substituted. |
| **Subagent** | Ask the agent to delegate to `test-writer` via the `task` tool. |
| **Hook** | After the agent edits a `.py` file, `post_edit` runs `ruff check --fix .` (non-blocking). `pre_commit` with `blocking: true` aborts on non-zero exit. |
| **MCP** | Uncomment the block in `.jarn/config.yaml` (or merge from `mcp-filesystem.snippet.yaml`), restart. MCP tools appear alongside built-ins. A failed server is skipped; runtime may show `degraded` — per-server `health` is mirrored on config but not shown in the toolbar yet. |

### 4. Debug permissions

- Default mode after setup is **`ask`** — shell, MCP, and mutating tools prompt for approval.
- **`auto-edit`** auto-allows file edits and read-only network tools (`web_search`,
  `web_fetch`, async status polling); shell and MCP still ask.
- Use `/mode` or `Shift+Tab` to switch modes at runtime.
- Allow rules in `.jarn/config.yaml` reduce prompts for trusted read-only commands
  (see the example `permissions.allow` block).

Full reference: [PERMISSIONS.md](PERMISSIONS.md).

### Known limitations

- **MCP toolbar indicator:** a failed MCP server surfaces in scrollback (`health=degraded`),
  in `jarn doctor` (per-server health), and via `/mcp status` (per-server health + last
  error, available at runtime). A persistent toolbar indicator is not shipped yet.

The sections below document each surface in full.

## 1. Skills

Reusable knowledge or workflows. A skill is a markdown file with frontmatter.

```markdown
---
name: run-migrations
description: Apply and verify database migrations safely.
trigger: auto            # auto | manual | "<keyword/glob>"
---
When asked to run migrations:
1. Back up the schema first.
2. Run the migration command.
3. Verify with the project's test suite before reporting done.
```

**Trigger semantics (hybrid model):**

- `auto` — the description is injected into the system prompt; the model decides when
  to use the skill (then reads the file for full instructions).
- `manual` — excluded from the auto catalog; only used when invoked explicitly.
- a keyword/glob string — auto-eligible and explicitly invokable.

List loaded skills with `/skills`. Use `trigger: manual` for skills with side effects
so they never fire on their own.

## 2. Slash commands

### Built-in commands

Shipped commands are declared in `src/jarn/extensibility/commands.py` as typed
`BuiltinCommand` entries in the `BUILTINS` tuple (`route: controller` →
`Controller._cmd_*`, or `route: repl` → handled in `repl.py`). `/help`, Tab
completion, and the README command table all derive from this registry — when adding
a built-in, update `BUILTINS` and keep `README.md` in sync
(`tests/test_phase3.py::test_readme_commands_match_registry`).

Current built-ins: `/help`, `/init`, `/config`, `/model`, `/mode`, `/sandbox`,
`/profile`, `/cost`, `/compact`, `/expand`, `/clear`, `/sessions`, `/resume`,
`/skills`, `/memory`, `/permissions`, `/mcp`, `/trust`, `/queue`, `/undo`, `/redo`,
`/checkpoints`, `/map`, `/wiki`, `/quit`. The README command table is the
authoritative list (kept in sync by the parity test). See
[README.md § Built-in commands](../README.md#built-in-commands).

### Custom commands

Custom `/command` prompt templates. The file body is sent to the agent with `$ARGS`
replaced by your arguments.

```markdown
---
name: explain
description: Explain how a piece of code works.
---
Explain how this code works, end to end. Cite file:line: $ARGS
```

`/explain the auth module` → sends *"Explain how this code works… : the auth module"*.
Custom commands cannot shadow built-ins (a `cost.md` loads as `/cost-custom`; and
`/commit` / `/review` are now built in, so a custom `review.md` loads as
`/review-custom`).

## 3. Custom subagents

Specialists the main agent delegates to via the `task` tool. The body is the
subagent's system prompt; `model` (optional) uses per-task routing; `tools`
(optional) restricts which tools the subagent may call.

```markdown
---
name: test-writer
description: Writes and runs unit tests for a given module.
model: openrouter/anthropic/claude-haiku-4-5
tools: [web_search]          # optional — limit extra (web/MCP) tools
---
You are a meticulous test engineer. Given a module, write thorough unit tests,
run them, and iterate until they pass. Report coverage gaps you couldn't close.
```

Subagents are converted to DeepAgents `SubAgent` specs at build time and appear to the
main agent as delegation targets.

**`tools:` restriction.** Omit `tools` and the subagent inherits the main agent's
full toolset. List tool names and the subagent is limited to that subset of the
*extra* tools — the built-in web tools (`web_search`, `web_fetch`) and any MCP
tools; an empty intersection means it gets no network/MCP tools at all. Unknown
tool names are rejected at startup so a typo fails fast. Note that the filesystem
built-ins (`read_file`/`write_file`/`edit_file`/`ls`/`glob`/`grep`) are always
available — they're enforced by the permission engine (and your mode), not by this
list — so `tools:` cannot make a subagent strictly read-only.

### Async / remote subagents

Beyond the markdown specialists, `config.async_subagents` declares remote/background
subagents reached over the DeepAgents Agent Protocol (`name`, `description`, `graph_id`,
optional `url` and `headers`). These are a *capability-granting* key, so an untrusted
project's async subagents are stripped until you trust the project (see
[CONFIGURATION.md](CONFIGURATION.md#project-trust)).

When async subagents are configured, the five async tools (`start_async_task`,
`check_async_task`, `update_async_task`, `cancel_async_task`, `list_async_tasks`) are
**gated through the permission engine** as network actions — see
[PERMISSIONS.md](PERMISSIONS.md#how-its-wired).

**Ambient-key advisory.** If you have a LangGraph/LangSmith/LangChain API key in your
environment (`LANGGRAPH_API_KEY` / `LANGSMITH_API_KEY` / `LANGCHAIN_API_KEY`), the
`langgraph_sdk` auto-attaches it as the `x-api-key` header to **any** async-subagent
`url`. For a non-local `url`, J.A.R.N. prints a one-time **warning** at session start
(also visible in the status line as a degraded state) naming the subagent and key. This
is **advisory detection, not prevention** — the SDK reserves `x-api-key` and exposes no
way to suppress the auto-load without breaking authenticating to a real LangGraph
Platform deployment, so the key is still sent. If a third-party `url` is not yours,
remove it or scope auth explicitly via the subagent's `headers`. Local urls
(`localhost`/`127.0.0.1`/`::1`) never warn.

## 4. Hooks

Shell commands J.A.R.N. runs automatically on lifecycle events — the backbone of the
self-verification loop. Declared in `config.yaml`:

```yaml
hooks:
  - event: post_edit
    command: "ruff check --fix ."
  - event: pre_commit
    command: "pytest -q"
    blocking: true          # non-zero exit aborts the commit
    matcher: "*.py"         # optional: only when the target matches
```

Events: `pre_tool`, `post_tool`, `post_edit`, `pre_commit`, `session_start`,
`session_end`. The triggering context is available to the command via
`$JARN_HOOK_EVENT` and `$JARN_HOOK_TARGET`. A *blocking* hook that exits non-zero
stops the action and remaining hooks for that event. An unknown/typo'd event name
(e.g. `sesion_start`) is rejected at load with a `ConfigError` rather than
silently never firing.

### Threat model — hooks run shell on your host

A hook is an arbitrary shell command J.A.R.N. runs **without asking**. Treat the
files that declare hooks as code you trust:

- **Project hooks** (`<repo>/.jarn/config.yaml`) are gated by the [project trust
  boundary](PERMISSIONS.md#project-trust-boundary): an *untrusted* repo's hooks
  are stripped, so simply opening a repository can't run shell. Trusting the repo
  opts in to its hooks — re-trust is re-triggered whenever the hook set changes.
- **Global hooks** (`~/.jarn/config.yaml`) always run — that's your own config,
  not untrusted input. If that file is compromised (e.g. a dotfile sync gone
  wrong, a shared machine), its hooks execute on `session_start` before you do
  anything. To require a one-time accept for the global tier, set
  `hook_global_require_trust: true` and run `jarn trust-hooks` once; until then
  the hook runner is disabled. Delete `~/.jarn/global-hooks.trusted` to re-gate.

### Environment: minimal allowlist by default

Hook subprocesses do **not** inherit your full `os.environ` — that would leak
every `*_API_KEY` / `*_TOKEN` you've exported into a hook script. By default a
hook sees only a minimal allowlist (`PATH`, `HOME`, `USER`, `SHELL`, `TMPDIR`,
`LANG`/`LC_*`, `TERM`) plus every `JARN_*` variable, the hook context vars
(`JARN_HOOK_EVENT`, `JARN_HOOK_TARGET`), and anything you declare via `extra_env`
at the call site.

To pass a secret to a hook on purpose, declare it explicitly rather than relying
on inheritance. To restore the old inherit-everything behavior (e.g. a hook that
needs a provider key you've exported), opt in globally:

```yaml
hook_inherit_env: true   # hook subprocesses inherit the full environment
```

This flag is stripped from *untrusted* project configs, so a repo you haven't
trusted can't turn env inheritance on to exfiltrate your secrets.

### Failures are surfaced, not swallowed

A failing hook is never silent: non-zero exits are logged at `WARNING` and, for
`pre_*`/`post_*` hooks, surfaced to the UI as a notice. A *blocking* `pre_commit`
/`pre_tool` hook that fails still rejects the action (e.g. tests fail → no
commit); a *non-blocking* failure is non-fatal — the action proceeds, you just
get told the hook failed.

## 5. MCP servers

Connect [Model Context Protocol](https://modelcontextprotocol.io) servers to give the
agent extra tools. Declared in `config.yaml`:

```yaml
mcp_servers:
  - name: filesystem
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
  - name: company-api
    transport: http              # streamable_http
    url: https://internal.example.com/mcp
    enabled: true
```

Tools from all enabled servers are loaded at session start and handed to the agent
alongside the built-ins. Loading is **isolated per server** (`get_tools(server_name=…)`
in its own try/except), so one server failing no longer drops every other server's
tools — the healthy ones still load. A failed server is logged and skipped, the runtime
is marked `degraded` with a `last_error` naming the failures, and each server's
last-known status is mirrored onto its config `health` field (`"ok"` / `"error"`, for
inspection only — no UI consumes it yet). A bad server never prevents the session from
launching.

## Tips

- Keep skill/command descriptions short and specific — they cost prompt tokens and
  drive auto-trigger quality.
- Commit project-tier extensions (`.jarn/`) so your whole team shares them.
- `state.sqlite` and `logs/` under `.jarn/` are gitignored by the provided
  `.gitignore`.

---

**Related docs:** [CONFIGURATION.md](CONFIGURATION.md) · [PERMISSIONS.md](PERMISSIONS.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [← docs index](README.md)
