# Permissions & Safety

> **Audience:** users who want to understand what J.A.R.N. will and won't do on their
> behalf, and contributors working on the authorization path. Start here before
> adjusting `permission_mode` or writing `allow`/`deny` rules.

Reliability is the whole point of J.A.R.N., and the permission system is where that
lives. Every file write and shell command the agent attempts is evaluated before it
runs. There is a trust boundary around the project, then three runtime layers.

## 0. Project trust boundary

A project's `.jarn/config.yaml` is **untrusted input** — opening a repository must not,
by itself, run code or leak secrets. Before J.A.R.N. honors any *capability-granting*
key from the project tier, it asks you to trust the project (once per root; you're
re-prompted if those keys change). Gated keys:

`hooks` · `mcp_servers` · `async_subagents` · `providers` · `execution` ·
`permission_mode` · `policy` · `observability` · `permissions.allow`

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

### Untrusted floor

On top of stripping capability keys, an **untrusted project is clamped to the
`review-only` profile** (read-only `plan` mode) at launch. This floor is one-way: it
cannot be loosened until you `jarn trust` the project — not via project config, not via
`jarn --profile ci`, not via `/profile`, and not via `/mode` or Shift+Tab (the mode
clamp lives in the single `apply_mode` choke point, and `/sandbox` is locked too). So
opening an untrusted repo lets the agent read and plan, but it can't write, run shell, or
escalate until you explicitly trust it.

## 0b. Policy profiles

A **profile** is a named bundle that sets `permission_mode` + `execution.local_sandbox`
+ `execution.sandbox_allow_network` + `policy.web_tools` together, so you pick one safe
posture instead of tuning four knobs:

| Profile | Mode | OS sandbox | Network | Web tools | For |
|---|---|---|---|---|---|
| `trusted-repo` | ask | off | on | on | everyday work you trust |
| `review-only` | plan (read-only) | off | on | on | reading/auditing unknown code |
| `sandbox-required` | ask | require | off | on | running untrusted code, isolated |
| `ci` | yolo (no prompts) | require | on | on | automation, isolated |
| `offline` | ask | auto | off | **off** | no network at all (web tools disabled) |

Select with `jarn --profile NAME`, `policy.profile` in config, or `/profile` at runtime.
**Precedence:** CLI `--profile` > `policy.profile` > raw settings; the untrusted floor
clamps last and always wins. `policy` is a capability-gated key (stripped from untrusted
project configs). `jarn doctor` shows the stored and effective profile. Note: `offline`
disables the **in-process** web tools (`web_search`/`web_fetch`) — they run in the agent
process and would otherwise bypass the OS sandbox's network denial.

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

### Edit before apply

For a file write/edit, the approval menu also offers **Edit before apply**: it opens
the proposed new content (the full file for `write_file`, the replacement text for
`edit_file`) in your `$EDITOR`. Save normally and the **edited** content is what lands
on disk — the turn resumes with the edited tool args, not the agent's original. Abort
the editor (e.g. a non-zero exit such as vim `:cq`) and the action is cancelled cleanly:
nothing is written.

> **Per-hunk approval is not implemented.** Edit-before-apply replaces the whole new
> content/replacement; you cannot accept some hunks of a diff and reject others. Per-hunk
> partial apply needs hunk parsing + partial diff application and is deferred (tracked by
> the `# TODO(per-hunk)` marker in `src/jarn/agent/session.py`).

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

When the gated action is a `write_file`/`edit_file`, the modal shows a colored unified
diff of the change. The diff is **capped at 40 lines** with a dim `… (+N more lines)`
footer, so creating or rewriting a large file doesn't flood the terminal — you're
approving the whole change, not reading it line by line.

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

For real isolation there are two routes. The **recommended default for untrusted repos**
is the lighter **OS sandbox** below (`execution.local_sandbox`) — it needs no extra
runtime and adds little latency. For stronger, container-grade isolation there is the
**opt-in Docker backend** (`execution.backend: docker`). Both **fail closed**: if the
requested isolation can't start, J.A.R.N. refuses to silently run on the host. Set
`execution.allow_local_fallback: true` to opt into host fallback — the status bar then
shows `host (no sandbox)` so the downgrade is never silent. The status bar always shows
the active isolation level (`docker` / `os-sandbox` / `host`), and `jarn doctor` reports it.

A remote `sandbox` backend (`execution.backend: sandbox`, LangSmith) is also available
when a remote runtime is configured.

### Docker backend (opt-in container isolation)

`execution.backend: docker` runs **every** shell command and filesystem operation inside a
Docker container. The host is exposed only through a bind-mount of the project root at the
same absolute path; everything else the agent touches is the container image's own
filesystem. Escaping requires a container breakout, not just slipping past the danger-guard
regex. Network is denied (`--network none`) when `execution.sandbox_allow_network: false`.
Resource limits (`docker_memory` / `docker_pids` / `docker_cpus`) and a non-root
`docker_user` are available (see [CONFIGURATION.md](CONFIGURATION.md)).

> **What Docker isolation does NOT protect.** It guards *the rest of the host* — not your
> repo. The project root is bind-mounted **read-write**, so code the agent runs can still
> read, modify, delete, or (with network on) exfiltrate anything inside the project itself,
> including `.git/` and any secrets committed there (`.env`, keys). Network isolation is
> **all-or-nothing** — turning it on to allow `pip`/`npm install` also re-enables
> exfiltration paths. The default image (`python:3.12-slim`) ships only python3 + `/bin/sh`;
> point `execution.docker_image` at an image with your toolchain (git/node/compilers) or
> in-container commands will fail. On Linux, leave nothing surprising: files the agent
> writes are owned by root unless you set `docker_user`. Docker is therefore an opt-in
> advanced option, not a substitute for the trust boundary + permission engine.

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

---

**Related docs:** [CONFIGURATION.md](CONFIGURATION.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [← docs index](README.md)
