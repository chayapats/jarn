# Permissions & Safety

> **Audience:** users who want to understand what J.A.R.N. will and won't do on their
> behalf, and contributors working on the authorization path. Start here before
> adjusting mode, sandbox, or trust settings.

Reliability is the whole point of J.A.R.N., and the permission system is where that
lives. Every file write and shell command the agent attempts is evaluated before it
runs. The model is three orthogonal concepts plus one shortcut:

> **Mode = how much I approve · Sandbox = where code runs · Trust = the gate · Presets = shortcuts that set the first two.**

- **Mode** — the live approval axis: how much the agent may do without prompting you.
- **Sandbox** — where code runs and what network access it has.
- **Trust** — the gate that activates capability keys and clamps an untrusted project to a safe floor.
- **Presets** — launch-time shortcuts that expand to a (Mode + Sandbox) combination once, then print what they set. They are not a persistent fourth axis.

---

## 1. Trust — the gate

### Project trust boundary

A project's `.jarn/config.yaml` is **untrusted input** — opening a repository must not,
by itself, run code or leak secrets. Before J.A.R.N. honors any *capability-granting*
key from the project tier, it asks you to trust the project (once per root; you are
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

Grant trust with `jarn trust` in the shell or `/trust` in the REPL.

### Untrusted floor

On top of stripping capability keys, an **untrusted project is clamped** directly to
`mode=plan` plus the review-only sandbox posture (OS sandbox off, network on, web on).
This floor is **one-way**: it is applied last at the launch boundary and can never be
loosened until you explicitly `jarn trust` the project — not via project config, not
via `--preset ci`, not via `/mode`, and not via Shift+Tab (the clamp lives in the
single `apply_mode` choke point; `/sandbox` is locked too). Opening an untrusted repo
lets the agent read and plan, but it cannot write, run shell, or escalate.

---

## 2. Mode — the approval axis

Set with `--mode` on the CLI, `permission_mode` in config, `/mode` in the REPL, or
Shift+Tab to cycle at runtime. (`--permission-mode` is kept as a hidden CLI alias.)

| Mode | Reads | Writes | Shell | Network |
|---|---|---|---|---|
| `plan` | allow | **deny** | **deny** | **deny** |
| `ask` (default) | allow | ask | ask | ask |
| `auto-edit` | allow | allow *(in-scope)* | ask | allow *(read-only: web + async status)* |
| `yolo` | allow | allow | allow | allow |

"In-scope" means inside the project root. An out-of-scope write is never silently
allowed; in `auto-edit` it downgrades to *ask*.

Built-in web tools (`web_search`, `web_fetch`) auto-allow in `auto-edit`; other
network (MCP, async subagents) still prompt `ask`.

### Plan-mode handoff

`plan` is not a dead end. In plan mode the agent researches read-only, then calls
the `exit_plan_mode` tool to present a concrete plan. You review it and pick how to
proceed (arrow-key picker): **auto-edit**, **ask**, or **keep planning**. On
approval the live mode is escalated through the same clamped choke point as every
other mode change (`Controller.apply_mode`) — so an **untrusted project can never
be escalated past the `plan` floor** this way; the picker explains that `/trust` is
required. `exit_plan_mode` is the one gated tool that is *callable in plan mode*
(the session driver routes it to the approver instead of letting the engine deny it
like any other non-read action). The default landing mode is `plan.exit_mode`
(default `auto-edit`).

### Agent-suggested memory

The agent can propose a durable memory with the `suggest_memory` tool. Like
`exit_plan_mode`, the session driver routes it straight to the approver (it never
mutates the store itself), so it is safe in any mode. You see a **"Save this memory?"**
prompt — save, edit-then-save (opens the body in `$EDITOR`), or decline. On approval
the memory is written through the same store and tier gating as `/memory add`: a
**project** write is refused on an **untrusted project** (the prompt explains `/trust`);
declining writes nothing.

---

## 3. Sandbox — where code runs

The Sandbox axis controls the execution environment for shell commands and the network
access available to them. Configure it with `execution.local_sandbox`,
`execution.sandbox_allow_network`, and `policy.web_tools`; select a posture at runtime
with `/sandbox`.

### Default: host backend

By default commands run on your **host** — `virtual_mode` adds path guardrails for
filesystem ops but does **not** sandbox shell execution. Real protection comes from the
project trust boundary first, then the permission engine and danger-guard on every
action. For real isolation there are two routes.

### OS sandbox (recommended for untrusted repos)

`execution.local_sandbox` adds an optional kernel-enforced layer **beneath** the
danger-guard for the local backend — no extra runtime, minimal latency.

- **macOS**: uses `sandbox-exec` (Seatbelt / SBPL) to deny `file-write*` outside the
  project root, system temp, and caches; optionally denies `network*`.
- **Linux**: uses `bwrap` (Bubblewrap) to bind-mount `/` read-only, overlay the project
  read-write, and optionally remove the network namespace (`--unshare-net`).

Three modes control behaviour:

| Value | Effect |
|---|---|
| `off` (default) | Disabled; existing behavior is preserved exactly. |
| `auto` | Use the OS sandbox when available; emit a one-time warning and continue without isolation if the tool is absent. |
| `require` | Sandbox or fail closed — `execute()` returns exit-code 126 with a clear error if the sandbox tool is not on PATH. Never runs unsandboxed silently. |

The writable scope is: the project root, the system temp dir (`$TMPDIR`/`/tmp`), and
common cache directories that exist (`~/.cache`, `~/.npm`, `~/.cargo`,
`~/.local/share`). Add extra paths with `execution.sandbox_writable`. Reads are always
unrestricted — only writes are limited.

Enable with `execution.local_sandbox: auto` to get kernel enforcement opportunistically,
or `require` in environments where isolation is non-negotiable. `jarn doctor` reports
the detected backend and configured mode.

### Docker backend (opt-in container isolation)

`execution.backend: docker` runs **every** shell command and filesystem operation inside
a Docker container. The host is exposed only through a bind-mount of the project root
at the same absolute path; everything else the agent touches is the container image's
own filesystem. Escaping requires a container breakout, not just slipping past the
danger-guard regex. Network is denied (`--network none`) when
`execution.sandbox_allow_network: false`. Resource limits (`docker_memory` /
`docker_pids` / `docker_cpus`) and a non-root `docker_user` are available (see
[CONFIGURATION.md](CONFIGURATION.md)).

> **What Docker isolation does NOT protect.** It guards *the rest of the host* — not
> your repo. The project root is bind-mounted **read-write**, so code the agent runs can
> still read, modify, delete, or (with network on) exfiltrate anything inside the project
> itself, including `.git/` and any secrets committed there (`.env`, keys). Network
> isolation is **all-or-nothing** — turning it on to allow `pip`/`npm install` also
> re-enables exfiltration paths. The default image (`python:3.12-slim`) ships only
> python3 + `/bin/sh`; point `execution.docker_image` at an image with your toolchain
> (git/node/compilers) or in-container commands will fail. On Linux, files the agent
> writes are owned by root unless you set `docker_user`. Docker is therefore an opt-in
> advanced option, not a substitute for the trust boundary + permission engine.

### Remote sandbox backend

A remote `sandbox` backend (`execution.backend: sandbox`, LangSmith) is also available
when a remote runtime is configured.

### Fail-closed guarantee

Both the OS sandbox and Docker backend **fail closed**: if the requested isolation
cannot start, J.A.R.N. refuses to silently fall back to the host. Set
`execution.allow_local_fallback: true` to opt into host fallback — the status bar then
shows `host (no sandbox)` so the downgrade is never silent. The status bar always shows
the active isolation level (`docker` / `os-sandbox` / `host`), and `jarn doctor`
reports it.

### Web-tools network knob

`policy.web_tools` controls the in-process web tools (`web_search`, `web_fetch`).
These run inside the agent process and would otherwise bypass the OS sandbox's network
denial, so they are disabled separately when network must be off (e.g. the `offline`
preset). `web_fetch` additionally enforces an SSRF guard (blocks
loopback/private/link-local/CGNAT/cloud-metadata targets, re-checked on every redirect;
streams with a byte cap; honors a `JARN_WEB_FETCH_ALLOW_HOSTS` allowlist).

---

## 4. Fine-grained rules and edit-before-apply

### Fine-grained rules

Layered *under* the mode, so you stop being asked about things you trust.

```yaml
permissions:
  allow: ["git status", "npm test", "ls *"]   # auto-allowed (globs supported)
  deny:  ["curl *", "ssh *"]                   # always blocked
```

Shell rules are matched against the command and against a generalized
"program + first arg" form, so allowing `npm test` covers repeated runs. **Wrapper and
eval commands are not generalized** — approving `bash -c "pytest"` (or any
`python -c …` / flag-led command) remembers the *exact* command, never a `bash -c`
blanket rule that would allowlist arbitrary payloads. At runtime an approval can be
remembered as:

- **once** — this single call,
- **session** — until you quit (in-memory),
- **always** — written to the project `.jarn/config.yaml` allowlist.

The interactive approval menu accepts a **single keypress** as well as the
`↑/↓ · Enter` arrow picker: press **`y`** or **`a`** to allow once, **`n`** or
**`d`** to deny — no arrow+Enter needed across a multi-edit turn. The arrow
picker (and the **View full diff** / **Edit before apply** options) still work
exactly as before.

### Edit before apply

For a file write/edit, the approval menu also offers **Edit before apply**: it opens
the proposed new content (the full file for `write_file`, the replacement text for
`edit_file`) in your `$EDITOR`. Save normally and the **edited** content is what lands
on disk — the turn resumes with the edited tool args, not the agent's original. Abort
the editor (e.g. a non-zero exit such as vim `:cq`) and the action is cancelled
cleanly: nothing is written.

When the gated action is a `write_file`/`edit_file`, the modal shows a colored unified
diff of the change. The diff is **capped at 40 lines** with a dim `… (+N more lines)`
footer, so creating or rewriting a large file doesn't flood the terminal — you are
approving the whole change, not reading it line by line.

> **Per-hunk approval is not implemented.** Edit-before-apply replaces the whole new
> content/replacement; you cannot accept some hunks of a diff and reject others. Per-hunk
> partial apply needs hunk parsing + partial diff application and is deferred (tracked by
> the `# TODO(per-hunk)` marker in `src/jarn/agent/session.py`).

---

## 5. The danger-guard (hard floor)

The guard runs *before* modes and rules and cannot be bypassed by an allowlist.

| Level | Behavior | Examples |
|---|---|---|
| `BLOCKED` | refused outright, un-allowlistable | `rm -rf /`, fork bomb, `mkfs`, `dd of=/dev/sda` |
| `DANGEROUS` | always confirm, even in YOLO; cannot be remembered "always" | `rm -rf <dir>`, `git push --force`, `git reset --hard`, `sudo`, `curl … \| sh`, out-of-scope or `.ssh`/credentials writes |
| `SAFE` | defer to modes/rules | everything else |

`rm` is classified by **flag presence**, not one positional pattern, so split
(`rm -r -f /`) and long (`rm --recursive --force /`) forms are caught the same as
`rm -rf /`; a recursive delete of a bare `/`, `/*`, `~`, `$HOME`, or `${HOME}` is
BLOCKED even without `-f`. `git` rules tolerate flags between the verb and
subcommand, so `git -C /repo reset --hard` is still flagged. Recursive `chmod`/`chown`
detect `-R` anywhere in the argv (so `chmod 777 -R .` and `chmod -R 777 .` both
flag). The command string is NFKC-normalized and run through a best-effort
homoglyph table before matching, so a disguised verb (e.g. Cyrillic `rm`) is
still caught. The guard is **conservative by design** — over-asking is the safe
failure mode. The authoritative pattern list is in `src/jarn/permissions/guard.py`.

> **The guard is a net, not a sandbox.** It inspects the pre-shell command string
> with patterns; it does not parse shell syntax. A payload can be hidden from
> these patterns by chaining through an interpreter — `eval`, `bash -c`,
> `python -c`, heredoc bodies, `$(printf …)`, or a `base64 -d | sh` indirection
> the net does not recognise. For code you do not trust, run it with
> `execution.backend: docker` or `execution.local_sandbox: require` — do not run
> untrusted code on the host in `yolo` and rely on this net. We do not claim the
> pattern set is complete. See [SECURITY.md](../SECURITY.md).

---

## 6. Decision precedence and how it's wired

### Precedence

```
0. reads                  → ALLOW (always)
1. guard BLOCKED          → DENY  (cannot be allowlisted)
2. explicit deny rule     → DENY
3. guard DANGEROUS        → ASK   (force confirm, even in YOLO)
4. allow / remembered     → ALLOW
5. coarse mode            → ALLOW | ASK | DENY
[launch boundary] untrusted floor → clamp mode=plan + review-only sandbox posture (applied last, always wins)
```

### How it's wired

DeepAgents' HITL middleware interrupts on every gated tool. J.A.R.N. gates **all
mutating tools in every mode** (`write_file`, `edit_file`, `execute`) plus the
built-in **web tools and any MCP tools** — so the engine, not the interrupt map,
decides the verdict and the danger-guard inspects every edit even in auto-edit/yolo
(an in-scope edit simply auto-resolves to ALLOW without a prompt).
`SessionDriver` catches each interrupt, builds an `Action`, runs the
`PermissionEngine`, and resumes the graph with an approve/reject decision —
auto-resolving ALLOW/DENY and only surfacing the modal for ASK. An `always` approval
is persisted to `.jarn/config.yaml` (comment-preserving), except for guard-dangerous
actions which can never be remembered. See
[ARCHITECTURE.md](ARCHITECTURE.md#the-turn-lifecycle).

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

This means the engine is the single source of truth for authorization, and it is fully
unit-tested (`tests/test_permissions.py`, `tests/test_guard.py`,
`tests/test_agent_mocked.py`) independently of any model.

### Cancellation

Pressing **Esc** / **Ctrl+C** during a turn cancels the agent task *and* kills any
shell command it spawned. Commands run in their own process session
(`CancellableLocalShellBackend`), so the whole process tree — not just the top-level
shell — is terminated; a long `sleep`/build won't keep running (or finish its side
effects) on the host after you cancel.

---

## Appendix: Presets

A **preset** is a launch-time shortcut that expands to a (Mode + Sandbox) combination
once and prints what it set. Presets are not a persistent axis — they are sugar over
Mode and Sandbox. Select with `jarn --preset NAME` or `/preset` in the REPL. To
persist a preset's effect, set Mode + Sandbox directly in config (a preset is just a
shortcut for those). `jarn doctor` shows the stored and effective settings.

**Precedence:** CLI `--preset` > the deprecated `policy.profile` config key > raw
settings; the untrusted floor clamps last and always wins.

| Preset | Mode | OS sandbox | Network | Web tools | For |
|---|---|---|---|---|---|
| `trusted-repo` | ask | off | on | on | everyday work you trust |
| `review-only` | plan (read-only) | off | on | on | reading/auditing unknown code |
| `sandbox-required` | ask | require | off | on | running untrusted code, isolated |
| `ci` | yolo (no prompts) | require | on | on | automation, isolated |
| `offline` | ask | auto | off | **off** | no network at all (web tools disabled) |

`offline` disables the **in-process** web tools (`web_search`/`web_fetch`) — they run
in the agent process and would otherwise bypass the OS sandbox's network denial.

### Deprecated aliases

`/profile`, `--profile` (CLI), and `policy.profile` (config key) are **deprecated
aliases** for the preset concept. They still work but emit a deprecation notice at
startup. The canonical names are `--preset` / `/preset`; `policy.profile` remains the
(deprecated) config key — there is no `policy.preset`, since a preset is a launch-time
shortcut, not a persistent axis. `--permission-mode` is kept as a hidden alias for
`--mode`.

---

**Related docs:** [CONFIGURATION.md](CONFIGURATION.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [← docs index](README.md)
