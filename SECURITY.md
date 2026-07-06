# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.5.x   | Yes (alpha — security fixes; no SLA) |
| 0.4.x   | Yes (alpha — security fixes; no SLA) |
| 0.1.x   | Best-effort only |

## Reporting a vulnerability

**Do not** open public GitHub issues for security bugs.

Email the maintainer with:

- Description and impact
- Steps to reproduce
- Affected version (`jarn --version`)
- Optional patch or PoC

We aim to acknowledge within **72 hours** and share a fix timeline when confirmed.

## Threat model (read this before running J.A.R.N.)

J.A.R.N. is a **local coding agent**. It can read/write files and run shell commands
in your project directory when you approve them (or automatically in permissive modes).

### Default posture

- **Host execution:** unless a sandbox runtime is configured and available, tools run
  on your machine with your user privileges. Safety is enforced by the permission
  engine and danger-guard, not by kernel isolation.
- **Permission modes:** default is **ask** — mutating actions prompt for approval.
  `yolo` disables prompts but **not** the hard danger-guard (e.g. `rm -rf /`).
- **Project trust:** a project's `.jarn/config.yaml` can declare hooks, MCP servers,
  provider overrides, and other capability keys. **Untrusted projects** have those keys
  stripped until you approve (`Trust this project's config?` or `jarn trust <path>`).
- **Secrets:** API keys live in `~/.jarn/config.yaml` or your OS keychain. Project
  config can reference `${ENV}` — only trust projects you would run code from.
  Inline plaintext keys in `config.yaml` are discouraged: the loader emits an
  `InlineSecretWarning` for any literal that looks like a real key, and rejects
  it outright when `strict_secrets: true` (recommended for CI / shared hosts).
  Prefer `keychain:jarn/<provider>`, `file:jarn/<provider>`, or `${ENV_VAR}`.
- **`JARN_HOME` override:** Global state (config, secrets, trust store, sessions) lives
  under `~/.jarn` by default. Setting `JARN_HOME` redirects all of that to another
  directory. A hijacked environment — a CI job, a shared shell, or instructions in an
  untrusted repo telling you to `export JARN_HOME=…` — can point secrets and trust
  decisions at an attacker-controlled path. Only set `JARN_HOME` in environments you
  control; `jarn doctor` warns when it is non-default.
- **Network:** `web_fetch` / `web_search` and MCP tools are gated through the permission
  engine. `web_fetch` blocks private/loopback/metadata addresses by default.
- **`@git:` mentions:** the four supported subcommands (`status`, `diff`, `staged`,
  `log`) run via a fixed, read-only argv allowlist (`git status --porcelain=v1 -b`,
  `git diff`, `git diff --staged`, `git log --oneline -15`).  The subprocess is called
  directly — **no shell interpolation**, no user-controlled arguments.  All output is
  passed through `redact_secrets` before injection.  Unknown subcommands are left
  verbatim; git errors produce an error block rather than exposing raw exceptions.
- **`@url:` mentions:** rewritten to a `web_fetch` instruction at submit time — **no
  pre-fetch occurs** in the REPL.  The agent's gated `web_fetch` tool (subject to the
  permission engine and SSRF guard) performs the actual network request.

### What we do not guarantee in v0.4 alpha

- Complete protection against a malicious **trusted** project (you approved its config)
- Sandbox isolation without an external sandbox provider
- Protection against prompt injection leading to social-engineered approvals —
  review approval prompts carefully

### The danger-guard is a net, not a sandbox

The danger-guard (`src/jarn/permissions/guard.py`) inspects the **pre-shell command
string** with patterns before the permission engine decides whether to run it. It
catches the common catastrophic shapes (`rm -rf /`, `mkfs`, force-push, pipe-to-shell,
privileged containers, package-manager postinstalls, mass working-tree discards, …)
and applies NFKC + best-effort homoglyph normalization so a disguised verb like
Cyrillic `rm` is still matched.

It does **not** parse shell syntax. A payload can be hidden from these patterns by
chaining through an interpreter — `eval`, `bash -c`, `python -c`, heredoc bodies,
`$(printf …)`, or a `base64 -d | sh` indirection the net doesn't recognise. The guard
is a defense-in-depth **net**; for code you do not trust, run it with
`execution.backend: docker` or the OS sandbox (`execution.local_sandbox: require`),
not on the host in `yolo`. We do not claim the pattern set is complete.

## Hardening checklist for operators

1. Run `jarn doctor` after cloning an unfamiliar repository.
2. Decline the trust prompt until you have reviewed `.jarn/config.yaml` and hooks.
3. Stay in `ask` or `plan` mode for untrusted codebases.
4. Set `execution.allow_local_fallback: false` if you require sandbox-or-nothing.
5. Keep `~/.jarn` permissions tight (`chmod 700 ~/.jarn`).

## Dependency security

Runtime dependencies are pinned in `uv.lock` for development. PyPI installs resolve
from `pyproject.toml` ranges. Report supply-chain concerns through the same private
channel above.
