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
cp examples/commands/review.md      .jarn/commands/
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
| **Command** | `/review the auth module` sends the review template with args substituted. |
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
name: review
description: Review the staged diff for bugs.
---
Review the staged git diff. Focus on correctness and edge cases: $ARGS
```

`/review the auth module` → sends *"Review the staged git diff… : the auth module"*.
Custom commands cannot shadow built-ins (a `cost.md` loads as `/cost-custom`).

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
stops the action and remaining hooks for that event.

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
