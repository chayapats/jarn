# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.11.x  | Yes (alpha — security fixes; no SLA) |
| 0.10.x  | Best-effort only |
| ≤ 0.9.x | No — upgrade to the current release |

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

### Installer supply chain

Release binaries are accepted only after their SHA-256 entry in the release manifest
matches. If the portable Python fallback needs `uv`, the installer downloads the
version-specific upstream installer URL, verifies its J.A.R.N.-pinned SHA-256 before
execution, and then relies on that verified upstream installer to check the selected
platform archive. A missing checksum tool, malformed digest, or mismatch fails closed
before the script is executed and leaves the prior J.A.R.N. installation active.

### Default posture

- **Host execution:** unless a sandbox runtime is configured and available, tools run
  on your machine with your user privileges. Safety is enforced by the permission
  engine and danger-guard, not by kernel isolation.
- **Permission modes:** default is **ask** — mutating actions prompt for approval.
  `yolo` disables prompts but **not** the hard danger-guard (e.g. `rm -rf /`).
- **Project trust:** a project's `.jarn/config.yaml` can declare hooks, MCP servers,
  provider overrides, and other capability keys. **Untrusted projects** have those keys
  stripped until you approve (`Trust this project's config?` or `jarn trust <path>`).
- **Secrets:** API keys live in environment variables, the OS keychain, or the
  permission-restricted J.A.R.N. file store; configuration should contain only a
  reference. Project config can reference `${ENV}` — only trust projects you would
  run code from.
  Inline plaintext credentials in `config.yaml` are rejected in every mode;
  the legacy `strict_secrets: false` setting cannot disable this invariant.
  Use `keychain:jarn/<provider>`, `file:jarn/<provider>`, or `${ENV_VAR}`.
  The file store behind `file:<service>/<account>` — `~/.jarn/secrets/` — is a
  **hard floor for the agent's path-addressed tools**: `read_file`, `ls`, `glob`,
  `grep`, `write_file` and `edit_file` are denied on it in every mode including
  `yolo`, above the allow tier, so no `allow` rule, remembered approval, or
  `sensitive_read_globs` setting can unlock it. Matching is by resolved path
  identity — it follows `$JARN_HOME`, catches a symlink by the file it names, and
  is case-insensitive so a `SECRETS` spelling cannot walk past it on a
  case-insensitive filesystem (APFS, NTFS). A spelling backstop denies any path
  ending in `.jarn/secrets` wherever it sits, so a project-level
  `<repo>/.jarn/secrets/` is treated the same way. Three boundaries worth knowing:
  - **Shell has a hard credential-store guard, but is still general-purpose.**
    Visible shell access to `.jarn/secrets` (including `$JARN_HOME/secrets`) is
    blocked in every mode. Other credential paths, dynamic expansion/evaluators,
    aliases/functions, environment dumps, uploads, and remote-transfer commands
    force an informed one-shot approval. Pattern recognition is defense in depth:
    a sufficiently obfuscated program can still perform equivalent syscalls. Run
    untrusted work under `execution.backend: docker` or a required OS sandbox.
  - **`~/.jarn/config.yaml` is not covered**, so an inline plaintext key there
    stays readable by the agent — another reason to prefer a secret reference
    over a literal.
  - **The `grep`/`glob` result filter is best-effort, not a boundary.** It is what
    stops a broad search whose *scope* looked benign from returning restricted
    content, but it works on tool output, and `grep` deliberately reports that it
    redacted something. Repeated literal probing can therefore still infer facts
    about a restricted file — that a given account exists, or that a string occurs
    in it — without the contents ever being surfaced. The hard controls remain the
    pre-execution gate above and OS-level isolation.
- **Global-tier permissions:** `~/.jarn` is created mode `0700` and re-tightened
  on every start, because it holds the prompt history, session transcripts, the
  wiki and memory the agent writes for itself, the trust store and conversation
  state. Created at the default umask it is `0755`, which exposes all of that to
  every other local account — negligible on a single-user laptop, not on a shared
  host or an always-on VPS. `jarn doctor` reports the mode when it could not be
  tightened (a directory owned by another user, or a filesystem with no POSIX
  modes). Windows is skipped: POSIX mode bits are not meaningful there. Note the
  limit of a mode check: `0700` describes the POSIX bits only. A filesystem ACL
  (macOS `chmod +a`, POSIX ACLs on Linux) can still grant another user access, and
  jarn neither inspects nor strips one — stripping could destroy a deliberate
  protective entry. Check with `ls -le` on macOS or `getfacl` on Linux if you
  share the host.
- **`JARN_HOME` override:** Global state (config, secrets, trust store, sessions) lives
  under `~/.jarn` by default. Setting `JARN_HOME` redirects all of that to another
  directory. A hijacked environment — a CI job, a shared shell, or instructions in an
  untrusted repo telling you to `export JARN_HOME=…` — can point secrets and trust
  decisions at an attacker-controlled path. Only set `JARN_HOME` in environments you
  control; `jarn doctor` warns when it is non-default.
- **Network:** `web_fetch` / `web_search` and MCP tools are gated through the permission
  engine. `web_fetch` blocks private/loopback/metadata addresses by default.
- **Remembered approvals:** approvals created by the UI are stored in a versioned
  scoped envelope containing action kind, originating tool/capability, exact target
  or safe command prefix, and resolved project workspace. They do not silently
  become another capability or follow a project rule into a different workspace.
  Legacy hand-authored string rules remain supported and should be reviewed as the
  broader explicit policy they are.
- **Pluggable search provider API keys:** `web_search` can be configured to use
  Tavily, Brave Search, or Exa instead of the keyless DuckDuckGo scraper.  Keys are
  always resolved through the existing secret-reference resolver (`${ENV_VAR}`,
  `keychain:jarn/<provider>`, `file:jarn/<provider>`) — inline literals are never
  accepted.  Resolved key values are never included in tool output strings.  The
  following HTTPS hosts are contacted only by the named provider clients (NOT through
  the `web_fetch` SSRF guard — they are fixed trusted API hosts, not user-supplied URLs):
    - `api.tavily.com`              — Tavily Search API
    - `api.search.brave.com`        — Brave Search API
    - `api.exa.ai`                  — Exa Search API
    - `html.duckduckgo.com`         — DuckDuckGo HTML scraper (SSRF-guarded, keyless fallback)
- **`@git:` mentions:** the four supported subcommands (`status`, `diff`, `staged`,
  `log`) run via a fixed, read-only argv allowlist (`git status --porcelain=v1 -b`,
  `git diff`, `git diff --staged`, `git log --oneline -15`).  The subprocess is called
  directly — **no shell interpolation**, no user-controlled arguments.  All output is
  passed through `redact_secrets` before injection.  Unknown subcommands are left
  verbatim; git errors produce an error block rather than exposing raw exceptions.
- **`@url:` mentions:** rewritten to a `web_fetch` instruction at submit time — **no
  pre-fetch occurs** in the REPL.  The agent's gated `web_fetch` tool (subject to the
  permission engine and SSRF guard) performs the actual network request.
- **`jarn login` (OpenRouter OAuth PKCE):** when you run `jarn login`, an HTTP server
  is bound to `127.0.0.1:<random-free-port>` for up to 300 seconds.  It serves a
  `/callback` endpoint and exits as soon as an authorization code is successfully
  redeemed (or the window closes).  The `callback_url` carries an unguessable path
  segment, and because any other local process could otherwise find the port and
  deliver a bogus code first, codes are queued and tried in turn — one from the
  nonce path first — rather than the first arrival winning outright.  An injected
  code cannot be redeemed with our verifier (that is what PKCE guarantees), so the
  only thing the race ever threatened was the login itself.
  Security properties: (a) bound to loopback only — no LAN exposure;
  (b) no client secret is used (public-client PKCE — RFC 7636 S256); (c) the
  authorization code and PKCE verifier are memory-only and never logged or stored;
  (d) the raw API key received from OpenRouter is passed directly to `store_secret` and
  is never written to `config.yaml` — only the opaque reference (`keychain:jarn/openrouter`)
  is persisted; (e) no secret value appears in the authorize URL (only the PKCE
  challenge is sent); (f) all printed output passes through `redact_secrets`.

### Telegram gateway (VPS)

The optional `jarn gateway` path (shipped in v0.10.0) expands J.A.R.N.'s network
boundary: an always-on Telegram bot can trigger the same project tools that a local
operator can. Treat the bot token, numeric user allowlist, VPS account, and every
allowlisted repository as privileged assets.

- **Single operator, private chats only.** Every message and callback is authorized by
  Telegram's numeric `from.id`; the allowlist is empty by default and startup refuses
  to continue without one. Group chats are rejected. A compromised allowlisted
  Telegram account is therefore equivalent to a remote operator within the gateway's
  remaining permission limits.
- **Global-only configuration.** Put `gateway:` only in `~/.jarn/config.yaml`.
  Project-tier blocks are stripped even for trusted projects, so cloning a repository
  cannot enable a daemon, replace its token, add an operator, or widen `/repo` roots.
- **Keep secrets out of Git and service definitions.** Prefer `${ENV}`,
  `keychain:…`, or `file:…` references. For systemd, use an `EnvironmentFile=` owned
  by the service user with mode `0600`; protect `~/.jarn` with mode `0700` and audit
  host ACLs separately. Do not expose the private gateway/worker NDJSON pipe as an API.
- **Permissions still apply.** Remote approvals are durable, but Telegram cards never
  offer permanent `ALWAYS`; the verdict path also downgrades it. The hard danger-guard
  remains active. This is not a sandbox: use Docker or the OS sandbox and avoid `yolo`
  on an internet-reachable VPS.
- **One poller and one worker per root.** A host flock rejects a second local poller;
  Telegram 409 is the cross-host fence. Per-root leases keep a gateway worker and a
  second gateway process from simultaneously owning the same root. Never call Telegram
  `logOut` to recover a conflict; stop the competing poller instead.
- **Backlog is not replayed.** Updates waiting at startup are reported and discarded,
  preventing stale messages from executing after a maintenance window. Worker death
  fails loud and does not auto-replay the interrupted turn.
- **Media is untrusted input.** Images and documents pass through the normal media and
  permission gates; staged files are deleted after use. Voice and unsupported media
  are refused. Continue to treat file contents and captions as prompt-injection input.

Deployment commands, stand-down exit codes, and a hardened systemd example are in
[docs/TELEGRAM_GATEWAY.md](docs/TELEGRAM_GATEWAY.md).

### GitHub Actions issue-fix bot (actor-allowlist requirement)

The `examples/github/issue-fix.yml` workflow runs J.A.R.N. in `yolo` mode and
pushes commits to a new branch when triggered by an issue comment containing
`@jarn`.  Because any user can comment on a public issue, this workflow **must**
gate on `github.event.comment.author_association`.  The example restricts
execution to `OWNER`, `MEMBER` (org members), and `COLLABORATOR`.  Removing or
weakening this guard lets arbitrary GitHub users execute code in your CI
environment and push branches to your repository.  Review the trust model in
[docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md) before enabling.

### Filesystem write scope (`--add-dir` multi-root)

The agent's write scope is bounded to a set of **roots** (the primary project root
first, plus any added with `jarn --add-dir <dir>` at launch or `/add-dir <path>`
mid-session). A write is in-scope only when it resolves under one of these roots; an
out-of-scope write is downgraded to *ask* (never silently allowed) and flagged
dangerous.

- **Per-root symlink discipline.** Each root's containment check follows symlinks
  (`Path.resolve()`), so a symlink placed *inside* an added root that points *outside*
  every root resolves out-of-scope and is rejected — the same escape defense that
  protects the primary root protects every added root. Relative write targets are
  anchored to the **primary** root, not the process CWD, so `../outside` is judged by
  intent.
- **One roots set, three enforcement points.** The engine's intent check, the local
  backend's virtual-mode filesystem guard, the OS sandbox (`sandbox-exec` / `bwrap`)
  writable allow-set, and the Docker bind mounts are all driven from the **same** roots
  set. An added-root write the engine allows is therefore also permitted at syscall
  time (no silent block), and a write outside all roots is denied at every layer
  (defense in depth against the TOCTOU window).
- **`/add-dir` is capability-gated.** Mid-session it requires explicit approval in
  `ask` mode and is **refused outright on an untrusted project** — a scope-widening
  capability is never granted to a repo whose config you have not trusted.
- **Checkpoint is primary-root only (a real limitation).** Auto-checkpoints, and thus
  `/undo` and `/rewind` file restore, snapshot the **primary project root only**. Edits
  the agent makes inside an **added** root are **not captured** and cannot be reverted
  through jarn — use your own VCS in those directories. `/add-dir` prints this warning
  when it adds a root. Project context (`JARN.md`) is likewise loaded from the primary
  root only.

### What we do not guarantee during alpha

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

It does **not** fully parse or emulate shell syntax. Known dynamic boundaries such as
command substitution, heredocs, inline interpreters, aliases/functions, dynamic
loaders, and decode/pipe-to-shell shapes are forced back to a one-shot approval, and
visible catastrophic commands still receive the stronger block. A payload can be
obfuscated beyond these patterns. The guard is a defense-in-depth **net**; for code
you do not trust, run it with
`execution.backend: docker` or the OS sandbox (`execution.local_sandbox: require`),
not on the host in `yolo`. We do not claim the pattern set is complete.

## Hardening checklist for operators

1. Run `jarn doctor` after cloning an unfamiliar repository.
2. Decline the trust prompt until you have reviewed `.jarn/config.yaml` and hooks.
3. Stay in `ask` or `plan` mode for untrusted codebases.
4. Set `execution.allow_local_fallback: false` if you require sandbox-or-nothing.
5. Keep `~/.jarn` permissions tight (`chmod 700 ~/.jarn`).
6. Use itemized `jarn uninstall` when leaving a machine. Only executable removal is
   selected by default; configuration, sessions, cache/telemetry, and credentials
   require separate choices. Codex-managed login and shared runtimes are preserved.

## Dependency security

Runtime dependencies are pinned in `uv.lock` for development. PyPI installs resolve
from `pyproject.toml` ranges. Release automation pins external actions to immutable
commit SHAs, emits SHA-256 manifests plus SPDX and CycloneDX SBOMs, and verifies
Sigstore/SLSA attestations before package publication when the repository supports
GitHub attestations. The installer/updater refuse integrity mismatches and keep the
active version until a candidate passes verification. Report supply-chain concerns
through the same private channel above.

The telemetry, tracing, support-report, and data-retention boundaries are documented
in [docs/PRIVACY.md](docs/PRIVACY.md).
