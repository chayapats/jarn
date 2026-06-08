# Configuration

J.A.R.N. reads two YAML files and merges them. Built-in defaults apply when a key is
absent, so a minimal config (or none at all) still works.

```
defaults  <  ~/.jarn/config.yaml (global)  <  <project>/.jarn/config.yaml (project)
```

- **Scalars and most lists**: the later tier replaces the earlier one.
- **`permissions.allow` / `permissions.deny`, `hooks`, `mcp_servers`**: concatenated,
  so a project *extends* global rules rather than replacing them.

The project root is the nearest ancestor of your CWD containing `.jarn/`, `JARN.md`,
or `.git/`. Set `JARN_HOME` to relocate the global directory (handy for testing).

## Project trust

A project's `.jarn/config.yaml` is **untrusted input** — opening a repo must not, by
itself, run code or leak secrets. So before J.A.R.N. honours any *capability-granting*
key from the project tier, it asks you to trust the project (once per root; you're
re-prompted if those keys change). The gated keys are:

`hooks` · `mcp_servers` · `async_subagents` · `providers` · `execution` ·
`permission_mode` · `permissions.allow`

Until you trust the project, those keys are **ignored** (the rest — `ui`, `context`,
`permissions.deny`, etc. — still applies) and the session continues safely. Decline if
you wouldn't run the repo's code: a malicious project could otherwise add a
`session_start` hook, spawn an MCP server, or point a provider `base_url` at an
attacker while referencing your real API-key env var. Trust decisions are stored in
`~/.jarn/trust.yaml` (keyed by project path + a fingerprint of the dangerous subset).
Project-tier prompt context is also skipped until trust is granted: project `JARN.md`,
project memory, skills, commands, and subagents do not load from an untrusted repo.

### Managing trust from the CLI

Besides the pre-launch prompt, the `jarn trust` subcommand manages the same trust store:

```bash
jarn trust                  # list trusted project roots (+ short fingerprint)
jarn trust /path/to/repo    # trust a root (validates .jarn/config.yaml, fingerprints it)
jarn trust /path/to/repo --remove   # untrust a root
jarn trust --json           # emit the trust list as JSON
```

`jarn trust <path>` requires the root to have a `.jarn/config.yaml`; it computes the
danger fingerprint and persists the entry. Both the command and the launch-time prompt
write through the same `~/.jarn/trust.yaml`, so they are a single source of truth.

## Generating config

`jarn setup` writes `~/.jarn/config.yaml` for you. To see a fully-commented template,
read `jarn.config.defaults.global_config_template()`.

## Validation

Config is validated strictly when it loads, so a typo fails loud instead of being
silently ignored. Errors raise `ConfigError` with the offending path.

- **Booleans** accept the obvious strings — `"false"`/`"no"`/`"off"`/`"0"`/`""` →
  `false`, the truthy equivalents → `true` — but anything else (e.g. `maybe`) is
  rejected rather than coerced via Python's loose `bool()`.
- **Numeric ranges** are checked: `budget.warn_at_pct` and `context.compact_at_pct`
  must be in `[0, 100]`; `budget.per_session_usd` must be `≥ 0`.
- **Type guards** on the structured sections (`providers`, `hooks`, `mcp_servers`,
  `async_subagents`) raise a clear `ConfigError` instead of crashing deeper in.
- **Unknown keys** at the **top level** are rejected against a fixed whitelist, with a
  path-aware message listing the expected keys. This is **top-level only** — unknown
  keys *nested* inside a section (e.g. extra provider fields) are not rejected; provider
  `extra` is folded through deliberately.

Run `jarn doctor` to surface these errors before a session; pass `jarn doctor --json`
to emit the diagnostics as machine-readable JSON.

## Full reference

```yaml
# Default provider profile and the model for the main agent loop.
default_profile: openrouter
default_model: openrouter/anthropic/claude-opus-4-8

# Coarse trust level: plan | ask | auto-edit | yolo
permission_mode: ask

# ── Providers ────────────────────────────────────────────────────────────
# Keys are referenced, never inlined:
#   ${ENV_VAR}                -> environment variable
#   keychain:jarn/<provider>  -> OS keychain (via `keyring`)
#
# Provider `type` is one of:
#   OpenAI-compatible (ChatOpenAI + base_url): openrouter, openai, lmstudio, groq,
#     deepseek, together, fireworks, xai, openai_compatible
#   Dedicated integrations: anthropic, ollama, google, mistral
providers:
  openrouter:
    type: openrouter
    api_key: ${OPENROUTER_API_KEY}
    base_url: https://openrouter.ai/api/v1
  groq:
    type: groq
    api_key: ${GROQ_API_KEY}
    base_url: https://api.groq.com/openai/v1
  google:
    type: google
    api_key: ${GOOGLE_API_KEY}
  my-local-server:
    type: openai_compatible    # any OpenAI-compatible endpoint
    api_key: ${MY_API_KEY}
    base_url: http://localhost:8000/v1
  anthropic:
    type: anthropic
    api_key: ${ANTHROPIC_API_KEY}
  openai:
    type: openai
    api_key: ${OPENAI_API_KEY}
  ollama:
    type: ollama
    base_url: http://localhost:11434
  lmstudio:
    type: lmstudio
    base_url: http://localhost:1234/v1

# ── Per-task model routing ───────────────────────────────────────────────
# Refs are <profile>/<model-id>. The FIRST segment is the provider profile; the
# rest is that provider's own model id and MAY contain slashes. So to use the
# OpenRouter model "deepseek/deepseek-v4-flash", the ref is:
#     openrouter/deepseek/deepseek-v4-flash
# (The setup wizard and the /model picker add the provider prefix for you — you
#  just type the model id, e.g. "deepseek/deepseek-v4-flash".)
routing:
  main: openrouter/anthropic/claude-opus-4-8        # main loop
  subagent: openrouter/anthropic/claude-haiku-4-5   # delegated subagents (cheaper)
  summarizer: openrouter/anthropic/claude-haiku-4-5 # context summarization
  fallback: []                                      # tried on primary failure

# ── Budget ───────────────────────────────────────────────────────────────
budget:
  per_session_usd: 5.0     # null = no limit; must be >= 0
  hard_stop: true          # stop the run when exceeded (false = warn only). The check
                           # is re-run after each streamed message (mid-turn, post-call),
                           # not before every individual model call.
  warn_at_pct: 80          # warn in the status bar at this fraction (0-100)

# ── Context management ───────────────────────────────────────────────────
context:
  auto_compact: true
  compact_at_pct: 85       # summarize when the context window is this % full (0-100)

# ── Execution backend ────────────────────────────────────────────────────
execution:
  backend: local           # local | sandbox  (toggle at runtime with /sandbox)
  sandbox_provider: langsmith   # sandbox runtime (needs external setup)
  multimodal: true         # read_file auto-detects image/PDF/audio/video
  allow_local_fallback: false   # if `backend: sandbox` can't start, run on the
                                # host anyway? OFF = fail closed (recommended).
                                # When on, the status bar shows "host (no sandbox)".

# ── Async / remote subagents (DeepAgents Agent Protocol) ─────────────────
async_subagents:
  - name: researcher
    description: Long-running web research, runs in the background.
    graph_id: research
    url: https://agents.example.com         # optional (remote)
    headers: {}                             # optional auth headers

# ── Permission rules (layered under permission_mode) ─────────────────────
permissions:
  allow:                   # auto-allow matching shell commands / paths (globs ok)
    - "git status"
    - "npm test"
  deny:                    # always blocked
    - "curl *"

# ── Hooks (lifecycle automation) ─────────────────────────────────────────
hooks:
  - event: post_edit       # pre_tool|post_tool|post_edit|pre_commit|session_start|session_end
    command: "ruff check --fix ."
  - event: pre_commit
    command: "pytest -q"
    blocking: true         # non-zero exit aborts the triggering action
    matcher: "*.py"        # optional glob on the tool target

# ── MCP servers (extra tools) ────────────────────────────────────────────
mcp_servers:
  - name: filesystem
    transport: stdio       # stdio | http (streamable_http) | sse
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
  - name: remote-tools
    transport: http
    url: https://example.com/mcp
# Tools are loaded per-server in isolation: one server failing to start no longer
# drops the others (see EXTENDING.md). After a load, J.A.R.N. mirrors each server's
# last-known status onto an optional `health` field ("ok" | "error"; default unset).
# It's informational only — no UI currently surfaces it.

# ── Observability ────────────────────────────────────────────────────────
observability:
  langsmith: false         # opt-in tracing (needs LANGSMITH_API_KEY)
  telemetry: false         # opt-in local usage analytics, default OFF (see ROADMAP)
  log_level: info          # debug | info | warning | error
  transcript: true         # append-only JSONL session transcript under .jarn/sessions/
                           # one event per line: user prompt, assistant reply, tool calls/results
                           # set false to disable; files are grep- and git-friendly

# ── UI ───────────────────────────────────────────────────────────────────
ui:
  theme: dark              # dark | light | high-contrast
  accent: cyan             # brand accent for splash + toolbar (cyan|blue|teal|…)
  # Set NO_COLOR=1 in the environment for plain/unstyled toolbar labels.
```

At REPL launch, `palette.configure_ui(theme, accent)` applies theme tokens to the
shared palette (chat colors, toolbar background/foreground, cost/context colors).
The bottom toolbar is rendered by `tui/toolbar.py` and shows **model · mode · queue ·
ctx · cost** (low-priority segments drop on narrow terminals).

## Cross-vendor interop (`compat`)

The `compat` section lets users coming from other agents (Claude Code, OpenAI
Codex, …) work out of the box without renaming their existing context files or
skill directories.

```yaml
compat:
  # Ordered list of context filenames to check in the project root.
  # The first file present wins. JARN.md is always tried first; AGENTS.md
  # (Codex / OpenAI) and CLAUDE.md (Claude Code) are fallbacks.
  context_files: ["JARN.md", "AGENTS.md", "CLAUDE.md"]

  # When true, skills and commands are also discovered from ~/.claude/skills,
  # ~/.claude/commands, <project>/.claude/skills, and <project>/.claude/commands
  # in addition to the canonical .jarn directories. .jarn always takes
  # precedence on a name conflict; built-in commands are never shadowed.
  # Project-tier .claude dirs respect the same trust gate as .jarn ones.
  read_claude_dir: true
```

Both settings have sensible defaults and the section can be omitted entirely.

## Secrets

| Form | Resolves to |
|---|---|
| `${OPENROUTER_API_KEY}` | the environment variable of that name |
| `keychain:jarn/openrouter` | `keyring.get_password("jarn", "openrouter")` |
| `sk-...` (literal) | itself (discouraged; avoid committing real keys) |

The wizard can store a pasted key in your OS keychain and write the
`keychain:jarn/<provider>` reference for you. Resolution failures surface a clear
message (and `jarn doctor` reports them per-provider).

## Pricing overrides

The cost estimate uses a built-in table (current as of June 2026). Override or extend
it with `~/.jarn/pricing.yaml`:

```yaml
"my-custom-model": { input: 1.5, output: 6.0 }   # USD per 1M tokens
```

Unknown models are counted as `$0` and flagged as *unpriced* in `/cost` so you know the
figure is incomplete rather than wrong.
