# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes (alpha — security fixes; no SLA) |

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
- **Network:** `web_fetch` / `web_search` and MCP tools are gated through the permission
  engine. `web_fetch` blocks private/loopback/metadata addresses by default.

### What we do not guarantee in v0.1 alpha

- Complete protection against a malicious **trusted** project (you approved its config)
- Sandbox isolation without an external sandbox provider
- Protection against prompt injection leading to social-engineered approvals —
  review approval prompts carefully

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
