# Configuration

> **Audience:** users setting up J.A.R.N. for the first time, and contributors
> adding new config keys. This is the authoritative reference for every setting.

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
`permission_mode` · `policy` · `observability` · `permissions.allow`

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

## Editing settings — `/config`

You rarely need to hand-edit the YAML. Inside the REPL:

- `/config` — open the **interactive settings panel** (Claude-Code style): category
  tabs run horizontally (**←/→**: General · Models · Policy · Execution · Budget ·
  Context · Features · UI), settings run vertically under the active tab (**↑/↓**).
  **Enter** toggles a boolean, cycles an enum, or edits a text/number value in place
  (type · Enter saves · Esc cancels); **Esc** closes. Each change saves immediately.
- `/config get <key>` — show one value (and its allowed choices, for enums).
- `/config set <key> <value>` — change a setting and **persist it to
  `~/.jarn/config.yaml`** (comments preserved, atomic write). The value is type-checked,
  the merged config re-validated, and the *combination* of settings checked for
  consistency (see [Validation](#validation)); an invalid or contradictory value is
  **rejected and rolled back**, and a valid one is applied to the running session
  immediately. A harmless-but-ineffective change is saved with a `⚠` note.

Keys are dotted, e.g. `/config set ui.theme light`, `/config set routing.main
openrouter/anthropic/claude-opus-4-8`, `/config set wiki.enabled true`,
`/config set budget.per_session_usd 5`.

Only a curated allowlist of **scalar** settings is editable this way (permission mode,
models/routing, policy profile, execution/sandbox, budget, context, ui, and feature
toggles). Structured / capability sections — `providers`, `hooks`, `mcp_servers`,
`async_subagents`, `permissions` — are **not** settable via `/config`; use `jarn setup`
or edit the file directly (and, for an untrusted project, `jarn trust` it first). Note:
on an untrusted project a permissive `permission_mode` is still persisted but the live
session stays clamped to the `review-only` floor.

> `/model`, `/mode`, `/sandbox`, `/profile` change the **current session only** and do
> not persist; use `/config set` (or edit the file) to make a change stick.

### Fixing a bad API key — `/key`

If a provider rejects your key mid-session (a `401`), run `/key` to fix it without
quitting. It prompts for the current provider's key, stores the secret in the OS
keychain, points the provider's `api_key` at a `keychain:jarn/<provider>` reference
(the secret is never inlined into config), and rebuilds the runtime so the next turn
uses the new key. The reference is persisted to `~/.jarn/config.yaml`, so it also
survives a restart. (You can pass the key inline as `/key <value>`, but it then lands
in your scrollback/history — prefer the bare `/key` prompt.)

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

### Cross-setting consistency

Beyond per-value checks, `/config` validates that settings make sense *together*
(`jarn.config.consistency`). Two severities:

- **Errors** — genuine contradictions where a knob can't take effect. The interactive
  editor refuses to *introduce* one (you can't turn the offending setting on). The
  canonical case: the kernel-level **OS sandbox** (`execution.local_sandbox` `auto`/
  `require`) is only honoured by the **local** backend, so enabling it while
  `execution.backend` is `docker`/`sandbox` is rejected. Only conflicts the current
  edit creates are blocked — a pre-existing, hand-edited contradiction never blocks an
  unrelated change, nor traps you from editing your way out of it.
- **Warnings** (`⚠`) — the value is harmless but currently has no effect, and is saved
  anyway: a `budget.hard_stop`/`warn_at_pct` while the session budget is `0` (unlimited),
  a `context.compact_at_pct` while `auto_compact` is off, a `context.repo_map_tokens`
  while the repo map is off, or a value that an active `policy.profile` will overwrite at
  launch.

In the panel the result line is colour-coded: `✗` (rejected), `⚠` (saved with a note),
`✓` (saved cleanly).

Run `jarn doctor` to surface these errors before a session; pass `jarn doctor --json`
to emit the diagnostics as machine-readable JSON.

## Full reference

```yaml
# Default provider profile and the model for the main agent loop.
default_profile: openrouter
default_model: openrouter/anthropic/claude-opus-4-8

# Coarse trust level: plan | ask | auto-edit | yolo
permission_mode: ask

# ── Policy profile ───────────────────────────────────────────────────────
# A named bundle of trust-relevant settings applied at launch. Selecting a
# profile overlays permission_mode + execution.local_sandbox +
# execution.sandbox_allow_network + policy.web_tools in one shot.
#   trusted-repo     — ask · no OS sandbox · network on · web tools on (everyday)
#   review-only      — plan (read-only) · web tools on
#   sandbox-required — ask · local_sandbox=require · network off (untrusted, isolated)
#   ci               — yolo (no prompts) · local_sandbox=require · network on
#   offline          — ask · local_sandbox=auto · network off · web tools OFF
# Precedence: `jarn --profile NAME` (CLI) > policy.profile (here) > raw settings.
# Untrusted projects are CLAMPED to `review-only` regardless — they can never be
# loosened (via config, --profile, /profile, /mode, or Shift+Tab) until trusted.
# Switch at runtime with `/profile`. `policy` keys are stripped from untrusted
# project configs (capability gate).
policy:
  profile: ""              # "" = none (use the raw settings above)
  web_tools: true          # register web_search/web_fetch? (a profile may flip this)

# ── Providers ────────────────────────────────────────────────────────────
# Keys are referenced, never inlined:
#   ${ENV_VAR}                -> environment variable
#   keychain:jarn/<provider>  -> OS keychain (via `keyring`)
# A literal key loads with a warning (or a hard error if strict_secrets: true).
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
  prompt_cache: auto   # auto | off. Cloud caching is automatic — the agent engine
                       #   adds Anthropic cache-control, and other cloud providers
                       #   cache by prefix server-side. The lever this controls is
                       #   the LOCAL keep-warm (keep_alive): auto applies it, off
                       #   skips it (cloud caching still stays on either way).
  keep_alive: 1800     # seconds to keep a LOCAL model + its KV/prefix cache
                       #   resident between turns. Maps to Ollama's keep_alive and
                       #   LM Studio's request ttl; without it those servers unload
                       #   on idle and drop the cache. 0 = leave to the provider.

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
  repo_map: tool            # off | tool | auto
                            # off  — repo map disabled entirely.
                            # tool — (default) a read-only `repo_map` tool is
                            #         registered; the model calls it on demand.
                            # auto — map is ALSO injected into the system prompt at
                            #         agent-build time (budget-capped) so the model
                            #         sees an overview immediately, without a tool call.
  repo_map_tokens: 1024     # token budget for the map (> 0). Applies to both
                            # the tool response and the system-prompt injection.

# ── Execution backend ────────────────────────────────────────────────────
execution:
  background: true         # register run/check/kill/list_background tools so the agent
                           #   can run a dev server / watcher / long build without blocking
                           #   the turn. Local backend only (a host process would escape a
                           #   container); /ps lists them. Gated like shell.
  backend: local           # local | docker | sandbox  (toggle at runtime with /sandbox)
                           # local  — run on the host (permission engine is the only
                           #          authorizer; NO isolation)
                           # docker — run every command + file op inside a Docker
                           #          container; the host is exposed only through a
                           #          bind-mount of the project root (REAL isolation)
                           # sandbox— remote runtime (LangSmith; needs external setup)
  sandbox_provider: langsmith   # remote sandbox runtime (or "docker" to redirect
                                # `backend: sandbox` to the local container backend)
  docker_image: python:3.12-slim  # image for `backend: docker`. Must ship python3 +
                                   # /bin/sh. Use a fuller image (node, ripgrep, git)
                                   # if your project needs those tools in-container.
  # Docker resource limits (backend: docker) — all unset by default.
  docker_memory: ""        # --memory cap, e.g. "2g" / "512m"  ("" = no cap)
  docker_pids: 0           # --pids-limit (0 = no cap). Set e.g. 512 for untrusted
                           # code to stop fork bombs without breaking normal builds.
  docker_cpus: ""          # --cpus cap, e.g. "2"  ("" = no cap)
  docker_user: ""          # --user uid:gid  ("" = image default, usually root).
                           # FOOTGUN on Linux: when empty, files the agent writes in
                           # the project land owned by root. Set to your host uid:gid
                           # (e.g. "1000:1000") to avoid root-owned files. Not forced
                           # by default because many images need root for apt/pip.
  multimodal: true         # read_file auto-detects image/PDF/audio/video
  allow_local_fallback: false   # if `backend: docker|sandbox` can't start, run on the
                                # host anyway? OFF = fail closed (recommended).
                                # When on, the status bar shows "host (no sandbox)".

  # OS-level kernel-enforced sandbox for the LOCAL shell backend.
  # Adds a second layer of isolation beneath the danger-guard using sandbox-exec
  # (macOS Seatbelt) or bwrap (Linux Bubblewrap). Shell commands can only write
  # inside the project (plus temp/caches) and optionally have no network.
  # Requires sandbox-exec (macOS, ships with Xcode CLI tools) or bwrap (Linux,
  # available in most distros as the `bubblewrap` package) on PATH.
  # Default "off" preserves the existing behavior exactly.
  local_sandbox: off       # off | auto | require
                           # off     — disabled (default; no behavior change)
                           # auto    — use when available, warn once + continue if not
                           # require — sandbox or fail closed (execute returns error 126)
  sandbox_allow_network: true    # set false to block outbound network in the sandbox
  sandbox_writable: []           # extra writable paths beyond project root + caches
                                 # e.g. ["/home/user/shared-build-cache"]

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

# ── Git safety (auto-checkpoint + /undo /redo) ────────────────────────────────
git:
  autocheckpoint: false    # set true to snapshot the working tree before each
                           # agent turn, enabling /undo and /redo.
                           # Snapshots live under refs/jarn/checkpoints/ and
                           # NEVER move HEAD, the branch, or the staged index.
                           # Requires a git repo with at least one commit.
  checkpoint_mode: shadow  # shadow (private refs only) | commit (reserved)
```

At REPL launch, `palette.configure_ui(theme, accent)` applies theme tokens to the
shared palette (chat colors, toolbar background/foreground, cost/context colors).
The bottom toolbar is rendered by `tui/toolbar.py` and shows **model · mode · queue ·
ctx · cost** (low-priority segments drop on narrow terminals).

## Wiki knowledge base (`wiki`)

A transparent, git-friendly per-project (and global) markdown knowledge base that the
agent can read, search, and write — complementing the existing vector memory.

```yaml
wiki:
  enabled: false   # set true to enable the four wiki tools on the agent
```

**Layout:**

| Path | Tier |
|---|---|
| `~/.jarn/wiki/pages/*.md` | global (always available) |
| `<project>/.jarn/wiki/pages/*.md` | project (gated by trust) |

A one-line-per-page `index.md` in each wiki dir is injected into the system prompt at
build time so the model knows what pages exist without calling a tool. Full pages are
read on demand via `wiki_read`.

**Tools registered when `wiki.enabled: true`:**

| Tool | Mutating | Permission |
|---|---|---|
| `wiki_search` | No | Always allowed |
| `wiki_read` | No | Always allowed |
| `wiki_write` | Yes | Prompted in `ask`; auto-allowed in `auto-edit`/`yolo` |
| `wiki_append` | Yes | Prompted in `ask`; auto-allowed in `auto-edit`/`yolo` |

`wiki_write` and `wiki_append` route through the permission engine exactly like
`write_file` / `edit_file` (mapped to `ActionKind.WRITE`), so the danger-guard and
per-session allow/deny rules apply.

**Trust gate:** project-tier wiki content (pages + index injection) is skipped when the
project is not trusted — the same boundary that gates `JARN.md`, skills, and project
memory. Global wiki is always available.

**Page name safety:** names are sanitized to a slug (letters/digits/hyphens/underscores).
Path traversal sequences (`..`) and path separators (`/`) are rejected, so a page name
can never escape the wiki directory.

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

### Context-file precedence

J.A.R.N. loads **exactly one** context file per project — whichever name it
finds first in the project root. The default resolution order is:

| Priority | Filename | Origin |
|---|---|---|
| 1 (highest) | `JARN.md` | J.A.R.N. native |
| 2 | `AGENTS.md` | OpenAI Codex / other agents |
| 3 (lowest) | `CLAUDE.md` | Claude Code |

**First present wins** — if `JARN.md` exists it is loaded and the others are
ignored, even if they also exist. At session start J.A.R.N. prints a one-line
notice naming the file that was loaded (e.g. `context: CLAUDE.md`) so you
always know which one is active.

To use a different file or order, set `compat.context_files` explicitly:

```yaml
compat:
  context_files: ["MYCONTEXT.md", "AGENTS.md"]
```

The project context file is subject to the same trust gate as all other
project-tier content — it is skipped (not injected) until the project is
trusted.

## Secrets

| Form | Resolves to |
|---|---|
| `${OPENROUTER_API_KEY}` | the environment variable of that name |
| `keychain:jarn/openrouter` | `keyring.get_password("jarn", "openrouter")` |
| `file:jarn/openrouter` | `~/.jarn/secrets/jarn/openrouter` (mode `0600`) |
| `sk-...` (literal) | itself — **discouraged**; triggers a load warning (or a hard error when `strict_secrets: true`). Avoid committing real keys. |

The wizard can store a pasted key in your OS keychain and write the
`keychain:jarn/<provider>` reference for you. On headless Linux (e.g. Raspberry
Pi over SSH) the OS keychain often has no backend — setup and `/key` then fall
back automatically to `file:jarn/<provider>` under `~/.jarn/secrets/`.
Resolution failures surface a clear message (and `jarn doctor` reports them
per-provider).

### Inline plaintext keys (`strict_secrets`)

J.A.R.N. is built around *referenced, never inlined* keys. If a provider's
`api_key` is a literal that looks like a real secret (an `sk-…`/`Bearer …`/PEM
block/vendor PAT, or a ≥32-char high-entropy string), the loader emits an
`InlineSecretWarning` at startup telling you to move it to a reference. Empty
keys and short local-server tokens (e.g. `lm-studio`) are left alone.

Set `strict_secrets: true` (top-level, default `false`) to turn that warning
into a hard `ConfigError` — useful in CI or shared environments where an inline
key should never load at all:

```yaml
strict_secrets: true
providers:
  openrouter:
    type: openrouter
    api_key: keychain:jarn/openrouter   # reference: always fine
```

The migration is one line per provider: replace the literal with `${ENV_VAR}`,
`keychain:jarn/<provider>`, or `file:jarn/<provider>`.

## Pricing overrides

The cost estimate uses a built-in table (current as of June 2026). Override or extend
it with `~/.jarn/pricing.yaml`:

```yaml
"my-custom-model": { input: 1.5, output: 6.0 }   # USD per 1M tokens
```

Unknown models are counted as `$0` and flagged as *unpriced* in `/cost` so you know the
figure is incomplete rather than wrong.

---

**Related docs:** [PERMISSIONS.md](PERMISSIONS.md) · [EXTENDING.md](EXTENDING.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [← docs index](README.md)
