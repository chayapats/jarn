# Permissions & Safety

Reliability is the whole point of J.A.R.N., and the permission system is where that
lives. Every file write and shell command the agent attempts is evaluated before it
runs. There is a trust boundary around the project, then three runtime layers.

## 0. Project trust boundary

A project's `.jarn/config.yaml` is **untrusted input** — opening a repository must not,
by itself, run code or leak secrets. Before J.A.R.N. honors any *capability-granting*
key from the project tier, it asks you to trust the project (once per root; you're
re-prompted if those keys change). Gated keys:

`hooks` · `mcp_servers` · `async_subagents` · `providers` · `execution` ·
`permission_mode` · `permissions.allow`

Until you trust the project these keys are **ignored** (the rest still applies) and the
session continues safely. Why it matters: otherwise a hostile repo could add a
`session_start` hook that runs shell the moment you open it, spawn a stdio MCP server
(an arbitrary command), or point a provider `base_url` at an attacker while referencing
your real `${API_KEY}` — exfiltrating it on the next model call. Decisions live in
`~/.jarn/trust.yaml` (project path + a fingerprint of the dangerous subset). See
[CONFIGURATION.md](CONFIGURATION.md#project-trust).

Project-tier prompt extensions are gated by the same boundary: until a project is
trusted, J.A.R.N. skips project `JARN.md`, project memory (`.jarn/memory`), project
skills, project commands, and project subagents. Global memory still loads.

## 1. Coarse modes

Set with `permission_mode` in config or `/mode` at runtime.

| Mode | Reads | Writes | Shell | Network |
|---|---|---|---|---|
| `plan` | allow | **deny** | **deny** | **deny** |
| `ask` (default) | allow | ask | ask | ask |
| `auto-edit` | allow | allow *(in-scope)* | ask | allow *(read-only network: web + async status)* |
| `yolo` | allow | allow | allow | allow |

"In-scope" means inside the project root. An out-of-scope write is never silently
allowed; in `auto-edit` it downgrades to *ask*.

## 2. Fine-grained rules

Layered *under* the mode, so you stop being asked about things you trust.

```yaml
permissions:
  allow: ["git status", "npm test", "ls *"]   # auto-allowed (globs supported)
  deny:  ["curl *", "ssh *"]                   # always blocked
```

Shell rules are matched against the command and against a generalized
"program + first arg" form, so allowing `npm test` covers repeated runs. **Wrapper and
eval commands are not generalized** — approving `bash -c "pytest"` (or any `python -c …`
/ flag-led command) remembers the *exact* command, never a `bash -c` blanket rule that
would allowlist arbitrary payloads. At runtime an approval can be remembered as:

- **once** — this single call,
- **session** — until you quit (in-memory),
- **always** — written to the project `.jarn/config.yaml` allowlist.

## 3. The danger-guard (hard floor)

The guard runs *before* modes and rules and cannot be bypassed by an allowlist.

| Level | Behavior | Examples |
|---|---|---|
| `BLOCKED` | refused outright, un-allowlistable | `rm -rf /`, fork bomb, `mkfs`, `dd of=/dev/sda` |
| `DANGEROUS` | always confirm, even in YOLO; cannot be remembered "always" | `rm -rf <dir>`, `git push --force`, `git reset --hard`, `sudo`, `curl … \| sh`, out-of-scope or `.ssh`/credentials writes |
| `SAFE` | defer to modes/rules | everything else |

`rm` is classified by **flag presence**, not one positional pattern, so split (`rm -r -f /`)
and long (`rm --recursive --force /`) forms are caught the same as `rm -rf /`; a recursive
delete of a bare `/`, `~`, `/*`, or `$HOME` is BLOCKED even without `-f`. `git` rules
tolerate flags between the verb and subcommand, so `git -C /repo reset --hard` is still
flagged. The guard is **conservative by design** — over-asking is the safe failure mode.
The authoritative pattern list is in `src/jarn/permissions/guard.py`.

## Decision precedence

```
1. guard BLOCKED      → DENY (cannot be allowlisted)
2. explicit deny rule → DENY
3. guard DANGEROUS    → ASK  (force confirm, even in YOLO)
4. allow / remembered → ALLOW
5. coarse mode        → ALLOW | ASK | DENY
```

Reads are always allowed (step 0).

## How it's wired

DeepAgents' HITL middleware interrupts on every gated tool. J.A.R.N. gates **all
mutating tools in every mode** (`write_file`, `edit_file`, `execute`) plus the
built-in **web tools and any MCP tools** — so the engine, not the interrupt map,
decides the verdict and the danger-guard inspects every edit even in auto-edit/yolo
(an in-scope edit simply auto-resolves to ALLOW without a prompt). Built-in web tools
(`web_search`, `web_fetch`) auto-allow in `auto-edit`; other network (MCP, async
subagents) still `ASK`. `SessionDriver` catches each interrupt,
builds an `Action`, runs the `PermissionEngine`, and resumes the graph with an
approve/reject decision — auto-resolving ALLOW/DENY and only surfacing the modal for
ASK. An `always` approval is persisted to `.jarn/config.yaml` (comment-preserving),
except for guard-dangerous actions which can never be remembered. See
[ARCHITECTURE.md](ARCHITECTURE.md#the-turn-lifecycle).

`web_fetch` additionally enforces an SSRF guard (blocks loopback/private/link-local/
CGNAT/cloud-metadata targets, re-checked on every redirect; streams with a byte cap;
honors a `JARN_WEB_FETCH_ALLOW_HOSTS` allowlist).

**Async-subagent tools are gated too.** When a (trusted) project configures
`async_subagents`, the five DeepAgents async tools — `start_async_task`,
`check_async_task`, `update_async_task`, `cancel_async_task`, `list_async_tasks` —
are added to the interrupt map so they route through the engine as
`ActionKind.NETWORK` → ASK, just like the web/MCP tools. (DeepAgents' HITL middleware
gates by tool *name* regardless of which middleware injected the tool, so this is a
real gate, not a no-op.) These tools exist only when async subagents are configured,
and the project trust boundary strips `async_subagents` from untrusted repos — so an
untrusted project never reaches this path. This closes the last of the trust-boundary
residuals: a trusted project's async subagent can no longer make remote calls that
bypass the engine.

This means the engine is the single source of truth for authorization, and it's fully
unit-tested (`tests/test_permissions.py`, `tests/test_guard.py`,
`tests/test_agent_mocked.py`) independently of any model.

## Cancellation

Pressing **Esc** / **Ctrl+C** during a turn cancels the agent task *and* kills any shell
command it spawned. Commands run in their own process session
(`CancellableLocalShellBackend`), so the whole process tree — not just the top-level
shell — is terminated; a long `sleep`/build won't keep running (or finish its side
effects) on the host after you cancel.

## A note on isolation

The default backend runs commands on your **host** — `virtual_mode` adds path guardrails
for filesystem ops but does **not** sandbox shell execution. So your real protection is,
in order: the **project trust boundary** (an untrusted repo can't supply commands/config
in the first place), then the **permission engine + danger-guard** on every action.

A `sandbox` backend (`execution.backend: sandbox`, `/sandbox on`) isolates execution when
a sandbox runtime is available. It **fails closed**: if the sandbox can't start, J.A.R.N.
refuses to silently run on the host. Set `execution.allow_local_fallback: true` to opt
into host fallback — the status bar then shows `host (no sandbox)` so the downgrade is
never silent.

### OS sandbox (optional kernel-enforced layer)

`execution.local_sandbox` adds an optional second layer of isolation **beneath** the
danger-guard for the local backend, enforced by the OS kernel rather than a regex:

- **macOS**: uses `sandbox-exec` (Seatbelt / SBPL) to deny `file-write*` outside the
  project root, system temp, and any caches; optionally denies `network*`.
- **Linux**: uses `bwrap` (Bubblewrap) to bind-mount `/` read-only, overlay the project
  read-write, and optionally remove the network namespace (`--unshare-net`).

Three modes control behaviour:

| Mode | Effect |
|---|---|
| `off` (default) | Disabled; existing behavior is preserved exactly. |
| `auto` | Use the OS sandbox when available; emit a one-time warning and continue without isolation if the tool is absent. |
| `require` | Sandbox or fail closed — `execute()` returns exit-code 126 with a clear error message if the sandbox tool is not on PATH. Does **not** silently run unsandboxed. |

The writable scope is: the project root, the system temp dir (`$TMPDIR`/`/tmp`), and
common cache directories that exist (`~/.cache`, `~/.npm`, `~/.cargo`,
`~/.local/share`). Add extra paths with `execution.sandbox_writable`. Reads are always
unrestricted — only writes are limited.

Enable with `execution.local_sandbox: auto` to get kernel enforcement opportunistically,
or `require` in environments where isolation is non-negotiable. `jarn doctor` reports the
detected backend and configured mode.
