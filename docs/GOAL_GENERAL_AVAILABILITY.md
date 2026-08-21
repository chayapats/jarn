# J.A.R.N. General-Availability Goal Specification

> Status: proposed implementation goal
> Updated: 2026-08-09
> Target: a general-user-ready J.A.R.N. release
> Priority language: **MUST** = release blocker, **SHOULD** = required unless a documented blocker exists, **MAY** = optional enhancement

## How to use this document

Give an implementation agent this file as the authoritative goal specification:

```text
Implement the J.A.R.N. General-Availability goal in
docs/GOAL_GENERAL_AVAILABILITY.md. Do not stop at planning. Complete the
implementation, tests, documentation, published-artifact validation, and
evidence report. Do not mark the goal complete while any MUST criterion remains
unfinished.
```

This document is standalone. The implementing agent must not need the original conversation to understand the observed failures, desired user experience, required work, or completion conditions.

## Official product baselines

The target experience is comparable to mature coding-agent CLIs such as Codex CLI and Claude Code: one-command installation, a visible and verifiable login ceremony, safe defaults, immediate time-to-value, discoverable commands, dynamic model selection, actionable errors, reliable updates, and no requirement that a general user understand the implementation stack.

Relevant current OpenAI baselines:

- [Codex CLI](https://developers.openai.com/codex/cli): standalone installation, first-run sign-in, interactive commands, permissions, model selection, and non-interactive execution.
- [Authentication](https://developers.openai.com/codex/auth): ChatGPT subscription and API-key modes, browser login, status verification, logout, caching, and credential storage.
- [Models](https://developers.openai.com/codex/models): account-available models, model switching, reasoning effort, recommended defaults, and model-specific choices.
- [Permissions](https://developers.openai.com/codex/permissions): least-privilege filesystem and network boundaries.

The exact available models, plans, protocol fields, prices, and product capabilities can change. J.A.R.N. must discover current account capabilities dynamically and must not treat this document's model examples as a permanent hard-coded catalog.

## Mission

Upgrade J.A.R.N. from a developer-oriented preview into a reliable CLI that a general user can install, authenticate, understand, repair, update, and use without learning Node.js, npm, Python, uv, PyInstaller, GLIBC, PATH precedence, provider-specific model IDs, app-server internals, or manual YAML configuration.

The implementation must include code changes, automated tests, platform testing, documentation, release-workflow changes, and an evidence-backed completion report. Planning alone does not satisfy this goal.

## Observed regression that must become a permanent test

The following real user journey is a P0 release blocker:

1. The machine is Ubuntu with glibc 2.35.
2. An older `jarn-cli` was previously installed globally through npm and resolves from `/usr/local/bin/jarn`.
3. The downloaded release binary starts through PyInstaller but requires `GLIBC_2.38`, so it cannot run.
4. `install.sh` correctly falls back to uv with managed Python 3.12 and installs a working executable at `~/.local/bin/jarn`.
5. The installer smoke-tests the absolute fallback path successfully.
6. The parent shell still resolves `jarn` to the old npm/PyInstaller binary.
7. The installer prints `Done`, but the user's next `jarn` command fails with the same GLIBC error.
8. During setup, the user selects ChatGPT subscription and confirms login.
9. No browser URL or device code appears.
10. A missing/incompatible Codex CLI or failed login is suppressed.
11. Setup returns success even though ChatGPT authentication is not valid.
12. The model picker exposes only static main/subagent/summarizer defaults, not the live account catalog.

This scenario must be represented in automated regression tests and a published-artifact canary. A release must not ship if any part regresses.

## Definition of general-user ready

A user on a clean supported machine must be able to:

1. Run one documented installation command.
2. Avoid installing Node, npm, Python, pip, pipx, or uv manually.
3. Avoid editing `PATH`, shell profiles, YAML, TOML, or JSON manually.
4. Choose a simple account path such as **Continue with ChatGPT**.
5. See a real browser URL or device-login URL and code.
6. Receive verified authentication status, auth mode, and plan/workspace information when available.
7. Receive a supported default model from the live account/provider catalog.
8. Start the first prompt without knowing a provider-specific model ID.
9. Understand every blocking error and receive an exact safe recovery command.
10. Repair, update, roll back, and uninstall without losing user data unexpectedly.

The standard journey from installation through the first prompt must not require external documentation. Before authentication begins, it should require no more than three meaningful decisions.

## Non-negotiable principles

### No false success

J.A.R.N. MUST NOT display `Done`, `Success`, `All set`, or return a success exit code when the user-visible executable, required dependency, authentication state, selected model, configuration, or first-run readiness is unverified or broken.

### Verify what the user will execute

Testing an absolute internal path is insufficient. Installation verification must include command resolution as experienced by the user, including PATH order, aliases, shell functions, cached command hashes, and older installations.

### Progressive disclosure

The normal path asks only what is required to start working. Provider internals, routing, custom endpoints, subagent models, summarizers, cache settings, and budget controls belong in an Advanced path.

### Safe by default

The default permission profile must follow least privilege, keep writes inside intended workspace roots, and require clear approval for broader or externally visible actions.

### Actionable errors

Every blocking error must explain what failed, why it likely failed, what remains safe, and the exact next command or action.

### Atomic and recoverable state

Installation, updates, config migration, session persistence, and repairs must not leave partially activated state. Previous working state must remain recoverable.

### Dynamic capability discovery

Authentication, model availability, reasoning efforts, and provider capabilities must be discovered from the current provider/account when possible. Static data is fallback metadata, not authoritative availability.

### No hidden billing transition

ChatGPT subscription use and usage-based API-key billing must be distinguished explicitly. J.A.R.N. must not silently move a user from one billing mode to another.

### Preserve user state

Install, update, repair, rollback, and uninstall must preserve configs, sessions, memory, extensions, and credentials unless the user explicitly approves their removal.

## A. Product UX and first run

### AC-UX-001 — Automatic onboarding

When `jarn` starts without a valid configuration, it MUST enter onboarding automatically without showing a Python traceback or implementation-specific exception.

### AC-UX-002 — Simple first screen

The first screen MUST prioritize understandable choices:

- Continue with ChatGPT
- Use OpenCode Go
- Use another cloud provider
- Use a local model

The first screen MUST NOT expose the entire provider registry as an undifferentiated list.

### AC-UX-003 — Existing-account detection

If a valid Codex-managed ChatGPT session exists, **Continue with ChatGPT** MUST be recommended and login MUST NOT be repeated unnecessarily.

### AC-UX-004 — Existing-key detection

If a recognized provider key exists in the environment, J.A.R.N. SHOULD offer it without displaying or copying the secret into config or logs.

### AC-UX-005 — Local-provider detection

If Ollama, LM Studio, or a configured local compatible endpoint is reachable, J.A.R.N. SHOULD offer it automatically and skip cloud-key prompts.

### AC-UX-006 — No mandatory model-ID knowledge

Standard onboarding MUST NOT require the user to type or recognize a model ID. The system MUST select a supported provider/account default.

### AC-UX-007 — Advanced path

An Advanced path MUST remain available for custom providers, endpoints, model routing, reasoning effort, fallbacks, budgets, themes, and permissions.

### AC-UX-008 — Back, cancel, and resume

Every onboarding step MUST support safe back/cancel behavior. `Ctrl+C`, `Esc`, timeout, or terminal closure MUST NOT leave a corrupt final config. An incomplete flow MUST be resumable at the missing step.

### AC-UX-009 — Explicit incomplete state

If configuration was drafted but a required dependency, login, or model validation did not complete, the UI MUST say **Setup incomplete**, identify the incomplete stage, and return non-zero when invoked as `jarn setup`.

### AC-UX-010 — Verified completion summary

Successful onboarding MUST summarize:

- active J.A.R.N. executable path;
- J.A.R.N. version and install method;
- provider and authentication mode;
- account plan/workspace when available;
- selected model and reasoning effort;
- permission profile;
- working directory;
- exact next command.

### AC-UX-011 — Immediate usability

After the documented one-line installation completes, the next documented user action MUST invoke the newly installed J.A.R.N., not an older executable.

### AC-UX-012 — Parent-shell limitation handled honestly

Because a `curl | sh` child cannot change its parent shell environment, the published command or activation design MUST explicitly solve this constraint. An acceptable documented single line is an installer followed by an outer-shell activation such as `&& exec "$SHELL" -l`. A generic “open a new shell” message alone does not satisfy the one-command objective.

### AC-UX-013 — Plain language

Standard onboarding MUST avoid unexplained terms such as app-server, model ref, managed Python, provider profile, or GLIBC. Advanced diagnostics may expose them with explanations.

### AC-UX-014 — Terminal compatibility

Interactive setup MUST work at 80x24, survive terminal resizing, and provide a plain-text fallback when full TUI capabilities are unavailable.

### AC-UX-015 — Unicode usability

Prompts, paths, project names, pasted content, and output MUST support Unicode and Thai text without corruption.

## B. Installer and command resolution

### AC-INSTALL-001 — One-command user-space install

The standard installer MUST install required J.A.R.N. components in user space and MUST NOT require `sudo` under normal conditions.

### AC-INSTALL-002 — Preflight detection

Before activation, the installer MUST detect:

- operating system and version;
- CPU architecture;
- libc implementation and version on Linux;
- shell and profile candidates;
- writable installation directories;
- sufficient disk space;
- required network/TLS capability;
- all discoverable existing J.A.R.N. installations;
- current command resolution;
- available supported package managers;
- interactive, SSH, headless, container, or CI context.

### AC-INSTALL-003 — Supported-platform contract

Tier-1 support MUST cover, or explicitly narrow with a documented product decision:

- Ubuntu 20.04, 22.04, and 24.04 x86_64;
- Debian 11 and 12 x86_64;
- supported Linux ARM64 distributions;
- WSL2 Ubuntu;
- macOS 13+ Apple Silicon;
- macOS Intel if advertised;
- native Windows 11 through `install.ps1`, or an early explicit redirect to WSL if native Windows is not supported.

Unsupported platforms MUST fail before downloading large artifacts and MUST explain the supported alternative.

### AC-INSTALL-004 — Oldest-runtime compatibility

A release binary MUST NOT require a glibc newer than the oldest supported Linux target. CI MUST execute the produced artifact inside that oldest target. Merely building successfully is not sufficient evidence.

### AC-INSTALL-005 — Automatic fallback

If a release binary cannot execute, the automatic method MUST remove the staged broken binary and install through a supported portable fallback without asking the user to diagnose GLIBC.

### AC-INSTALL-006 — Multi-layer verification

After installation, verification MUST include:

- installed absolute path with `--version`;
- a minimal non-mutating command;
- recorded install method and version;
- user-visible command resolution;
- detection of aliases, functions, and hashed paths where supported.

### AC-INSTALL-007 — Collision discovery

The installer and `jarn doctor` MUST detect commands installed through npm, pip user installs, pipx, uv tool, Homebrew, system packages, standalone binaries, aliases, shell functions, and cached shell command hashes.

### AC-INSTALL-008 — Collision blocks completion

If the shell would invoke a different executable, the installer MUST NOT print `Done`. It MUST show the new path, shadowing path, current precedence, safe cleanup choices, and an exact activation command.

### AC-INSTALL-009 — No silent deletion

Older installations MUST NOT be deleted automatically unless the user confirms the exact targets. A safe PATH-precedence fix is preferred over destructive cleanup.

### AC-INSTALL-010 — Transactional activation

Installation MUST follow a transaction:

1. resolve version;
2. download;
3. verify checksum/signature;
4. stage;
5. smoke-test;
6. atomically activate;
7. verify active resolution;
8. retain a rollback candidate.

### AC-INSTALL-011 — Preserve the working version

Failure at any step MUST preserve or restore the previous working executable.

### AC-INSTALL-012 — Failure-mode handling

Tests MUST cover interrupted downloads, signals, disk full, read-only targets, checksum mismatch, unavailable assets, proxy errors, DNS errors, TLS errors, and package-manager failures without damaging the prior installation.

### AC-INSTALL-013 — Artifact integrity

Release artifacts MUST have SHA-256 verification. Signed artifacts and build provenance SHOULD be provided for GA.

### AC-INSTALL-014 — Concise default output

Normal output MUST show major stages and final status. Full dependency package lists and debug output MUST be behind `--verbose` or written to a referenced log.

### AC-INSTALL-015 — Installer controls

The installer SHOULD expose:

- `--version`;
- `--channel stable|beta`;
- `--install-dir`;
- `--method auto|binary|python`;
- `--no-setup`;
- `--dry-run`;
- `--yes`;
- `--verbose`;
- `--help`.

Environment-variable equivalents may remain for automation.

### AC-INSTALL-016 — Idempotency

Re-running the same command MUST safely install, repair, or update. A verified current version MUST not be needlessly reinstalled.

### AC-INSTALL-017 — Published-artifact testing

At least one CI or release-canary path MUST fetch `install.sh` and release assets from their published URLs. Local fixtures alone are insufficient.

## C. Dependency management

### AC-DEP-001 — No development-stack prerequisite

A standard ChatGPT-subscription user MUST NOT need to install Node, npm, Python, pip, pipx, or uv manually.

### AC-DEP-002 — Codex CLI acquisition

When ChatGPT subscription is selected and Codex CLI is missing, J.A.R.N. MUST offer to install the official standalone Codex CLI using its official source.

### AC-DEP-003 — Transparent external dependency install

Before installing an external dependency, J.A.R.N. MUST show its name, purpose, source, destination, and version/channel.

### AC-DEP-004 — Compatibility range

J.A.R.N. MUST define and test a minimum compatible Codex CLI version. Protocol capabilities SHOULD be negotiated so newer versions do not fail merely because the version string is unfamiliar.

### AC-DEP-005 — Actionable outdated dependency

An incompatible old Codex CLI MUST produce an update offer or exact official command before login/model operations start.

### AC-DEP-006 — Isolated Python fallback

The Python fallback MUST use a managed isolated environment and MUST NOT mutate system Python or apt-managed packages.

### AC-DEP-007 — Diagnostic inventory

`jarn doctor` and machine-readable status MUST report relevant dependency paths and versions without exposing secrets.

## D. Authentication

### AC-AUTH-001 — Dependency check before login prompt

J.A.R.N. MUST verify that an appropriate Codex CLI is available before asking whether the user wants to sign in.

### AC-AUTH-002 — No suppressed authentication errors

Exceptions or non-zero results affecting Codex availability, login, callback, account verification, workspace access, or token refresh MUST be surfaced. They MUST NOT be swallowed while setup continues as successful.

### AC-AUTH-003 — Environment-appropriate flow

Desktop sessions SHOULD default to browser callback login. SSH, headless, container, or browserless sessions SHOULD default to device-code login. The user MUST be able to override the choice.

### AC-AUTH-004 — Visible ceremony

Browser login MUST show a fallback URL even if automatic opening succeeds. Device login MUST show verification URL, user code, expiry information when known, waiting status, cancellation instructions, and retry behavior.

### AC-AUTH-005 — Output passthrough

User-required output from Codex login MUST remain visible. TUI suspension/resume MUST not erase the only copy of a URL, code, or failure.

### AC-AUTH-006 — Post-login verification

A subprocess exit code of zero is not sufficient. J.A.R.N. MUST refresh account state and verify authenticated mode, account type, ChatGPT subscription versus API key, token usability, and plan/workspace metadata when exposed.

### AC-AUTH-007 — Billing-mode protection

If a user selects ChatGPT subscription but Codex is authenticated with an API key, J.A.R.N. MUST stop, explain separate API billing, and offer explicit recovery. It MUST NOT silently continue.

### AC-AUTH-008 — No undisclosed paid validation

Account and catalog checks SHOULD avoid billable model turns. Any real validation request that can incur usage MUST be labeled and require confirmation.

### AC-AUTH-009 — State model

Authentication MUST distinguish at least:

- dependency missing;
- incompatible dependency;
- signed out;
- login pending;
- authenticated with ChatGPT;
- authenticated with API key;
- expired or revoked;
- workspace denied;
- refresh failed;
- network unavailable;
- unknown protocol error.

### AC-AUTH-010 — Setup completion condition

ChatGPT subscription setup may return success only after the verified state is authenticated with ChatGPT.

### AC-AUTH-011 — Auth command consistency

Provide a consistent command family:

- `jarn auth login`;
- `jarn auth status`;
- `jarn auth logout`;
- `jarn auth repair`.

Existing `jarn codex ...` commands MAY remain as backward-compatible aliases.

### AC-AUTH-012 — Stable automation interface

`jarn auth status --json` MUST have a documented stable schema and meaningful exit codes.

### AC-AUTH-013 — Authentication test cases

Automated contract tests MUST cover missing CLI, outdated CLI, existing login, browser success/failure, device success/timeout, cancellation, non-zero subprocess exit, zero exit with signed-out account, API-key mismatch, expired credentials, refresh failure, workspace denial, network loss, and malformed protocol responses.

### AC-AUTH-014 — Scoped logout

Logout MUST remove only the selected authentication mechanism and MUST not delete unrelated provider keys.

## E. Model discovery and selection

### AC-MODEL-001 — Static defaults are not authoritative

`DEFAULT_MODELS` or equivalent static data MAY be used for bootstrapping, offline fallback, migrations, or display metadata, but MUST NOT be treated as proof that an account can use a model.

### AC-MODEL-002 — Codex live catalog

For Codex subscription, J.A.R.N. MUST call app-server `model/list`, follow cursor pagination until complete, and use visible account-available entries by default.

### AC-MODEL-003 — Hidden models

Hidden or experimental models MUST be excluded from the standard picker unless the user opens Advanced or explicitly requests them.

### AC-MODEL-004 — Rich catalog entries

When provided, the picker SHOULD display:

- display name and model ID;
- concise description;
- recommended/default state;
- account availability;
- preview/deprecated state;
- supported and default reasoning efforts;
- input modalities;
- context window;
- service or speed tiers;
- billing mode/cost context.

### AC-MODEL-005 — Entitlement truth

A model MUST NOT be labeled available unless the live provider/account or a clearly labeled cache supports that claim. Plan names alone MUST NOT be used to infer entitlement.

### AC-MODEL-006 — Automatic supported default

Standard onboarding MUST select the provider-reported default or a supported recommended model automatically.

### AC-MODEL-007 — Advanced manual entry

Advanced users MUST retain a manual model-ID option. It SHOULD be validated before persistence whenever the provider supports validation.

### AC-MODEL-008 — One catalog service

Setup, `/model`, routing configuration, doctor, and pre-turn validation MUST use the same model-catalog abstraction.

### AC-MODEL-009 — Model and reasoning together

`/model` MUST allow switching the model and supported reasoning effort. Unsupported effort choices MUST not be shown.

### AC-MODEL-010 — Routing validation

Changing provider/model MUST validate main, subagent, summarizer, and fallback routes. Retired or unavailable background routes MUST not remain silently configured.

### AC-MODEL-011 — Cache provenance

Catalog cache MUST include source, retrieval timestamp, TTL, schema version, account/provider identity in a privacy-preserving form, and stale status.

### AC-MODEL-012 — Offline honesty

If live discovery fails, a valid cache may be used with a visible cached timestamp. Static emergency data may be used only with an **Offline fallback; availability unverified** label.

### AC-MODEL-013 — Refresh

`/model refresh` or equivalent MUST perform a real refresh and report source, count, and cache status.

### AC-MODEL-014 — Retirement and migration

Retired or renamed models SHOULD produce a migration suggestion. Config MUST not be silently rewritten without notice and backup.

### AC-MODEL-015 — Pre-turn failure

An unavailable selected model MUST fail before agent execution with an actionable picker/replacement path, not after an opaque provider request.

## F. Main CLI and interactive experience

### AC-CLI-001 — Offline-capable help

`jarn --help` MUST explain installation state, common commands, login, model selection, permissions, repair, update, and support paths sufficiently for normal use without a browser.

### AC-CLI-002 — Concise session header

An interactive session SHOULD show current directory, model, reasoning effort, permission mode, provider/auth state, and context status without overwhelming the prompt.

### AC-CLI-003 — Command discoverability

Slash commands MUST be discoverable through `/help`, completion, and contextual hints. At minimum, support or clearly map:

- `/help`;
- `/status`;
- `/model`;
- `/permissions`;
- `/new`;
- `/resume`;
- `/compact`;
- `/cost`;
- `/review`;
- `/undo`;
- `/doctor`;
- `/login`;
- `/logout`;
- `/exit`.

### AC-CLI-004 — Unknown-command handling

Unknown slash commands MUST offer close matches and MUST not be accidentally sent to the model as ordinary user text.

### AC-CLI-005 — Cancellation semantics

The first `Ctrl+C` during work MUST cancel the active turn safely. A subsequent explicit exit action may close the program. Cancellation SHOULD stop UI activity, provider streams, and child processes within one second when the OS permits.

### AC-CLI-006 — Terminal robustness

The UI MUST handle resize, long paths, multiline paste, Thai combining characters, Unicode, narrow terminals, and disabled color. `TERM=dumb` MUST receive usable plain output.

### AC-CLI-007 — Non-interactive mode

Provide stable human and machine modes such as:

```text
jarn exec "task"
jarn exec --json "task"
```

JSON stdout MUST contain no spinner, ANSI escapes, or unrelated human prose.

### AC-CLI-008 — Exit-code taxonomy

Document distinct exit codes for success, usage/config error, auth failure, unavailable model, permission denial, network/provider failure, cancellation, update failure, and internal error.

## G. Permissions and safety

### AC-SAFE-001 — Least-privilege default

The default MUST keep writes inside intended workspace roots and require approval for broader access.

### AC-SAFE-002 — User-facing permission names

Expose understandable modes such as Read only, Ask before changes, Edit workspace, and Full access, mapped to internal policy without leaking unnecessary implementation details.

### AC-SAFE-003 — Informed approvals

An approval prompt MUST show the action/command, working directory, relevant paths, network destination when applicable, reason, and whether approval applies once or persists.

### AC-SAFE-004 — Scoped persistence

Persistent approval MUST be scoped by capability, command prefix, workspace, and relevant target. It MUST NOT become a wildcard silently.

### AC-SAFE-005 — Full-access warning

Full access/yolo MUST require an explicit warning and SHOULD NOT persist across sessions unless deliberately configured.

### AC-SAFE-006 — Bypass resistance

Tests MUST cover path traversal, symlink escape, secret-file reads, shell injection, command substitution, environment leakage, model-generated approval bypass, aliases, MCP tools, and provider bridge bypasses.

### AC-SAFE-007 — Protected secrets

`.env`, private keys, credential stores, `auth.json`, tokens, and configured secret patterns MUST be protected by default and redacted from model context/logs.

### AC-SAFE-008 — Network control

Network access MUST be disableable and SHOULD support domain-scoped policy.

### AC-SAFE-009 — Fail closed

Unexpected tool or bridge requests that bypass the permission engine MUST fail closed and create a redacted diagnostic event.

### AC-SAFE-010 — Recoverable edits

Workspace edits SHOULD have checkpoints. `/undo` MUST preview the affected changes and avoid reverting unrelated user work.

## H. Sessions, crash recovery, and user data

### AC-SESSION-001 — Atomic persistence

Every completed turn and important state transition MUST be persisted atomically.

### AC-SESSION-002 — Crash recovery

Process crash, forced termination, or power loss MUST not corrupt the session store. On restart, J.A.R.N. SHOULD offer the most recent incomplete session.

### AC-SESSION-003 — Useful resume list

`/resume` MUST show timestamp, project, model, short title, and complete/incomplete state.

### AC-SESSION-004 — Version compatibility

Supported minor upgrades MUST preserve session IDs and resume behavior or provide an explicit migration.

### AC-SESSION-005 — Audit context

Model changes, compaction, routing, delegated turns, tool approvals, and checkpoints SHOULD be represented sufficiently for recovery and explanation.

### AC-SESSION-006 — No secrets in transcripts

Sessions MUST NOT persist raw authentication tokens, API keys, or reversible masked secrets.

### AC-SESSION-007 — User control

Users MUST be able to list, export, and delete sessions. Uninstall MUST separately ask whether user data should be retained.

## I. Configuration and migration

### AC-CONFIG-001 — Versioned schema

Configuration MUST carry a schema version.

### AC-CONFIG-002 — Transactional migration

Each migration MUST validate the source, create a timestamped backup, write a temporary destination, validate it, atomically replace the original, and roll back on failure.

### AC-CONFIG-003 — Preserve customization

Updates and setup reruns MUST preserve custom providers, routes, permissions, themes, headers, and supported unknown extension keys.

### AC-CONFIG-004 — Corruption recovery

Corrupt config MUST produce a clear error, backup/recovery location, and repair choices. It MUST NOT be silently overwritten.

### AC-CONFIG-005 — Config commands

Provide or preserve:

- `jarn config show`;
- `jarn config path`;
- `jarn config validate`;
- `jarn config edit`;
- `jarn config reset`.

### AC-CONFIG-006 — Provenance

Status SHOULD explain whether important values come from built-in defaults, global config, project config, environment variables, CLI flags, or managed policy.

### AC-CONFIG-007 — Secret references

Secrets MUST be stored as references. File-based secret fallbacks MUST use restrictive permissions such as `0600` where supported.

## J. Errors, diagnostics, and self-healing

### AC-ERR-001 — Stable error codes

User-facing failures MUST carry stable codes, for example `JARN-AUTH-001`, organized by subsystem.

### AC-ERR-002 — Complete error anatomy

A blocking error MUST contain a short summary, detected/likely cause, affected component, retryability, exact recovery action, and log/report path.

### AC-ERR-003 — No default traceback

Raw tracebacks MUST be hidden from normal users and available only through debug mode or a redacted log.

### AC-ERR-004 — No material suppression

Exceptions affecting install, auth, config, catalog, selected model, permissions, or data integrity MUST not be suppressed into success.

### AC-ERR-005 — Bounded retry

Transient network/provider failures SHOULD use bounded backoff. Auth, permission, and config errors MUST not be retried indefinitely.

### AC-ERR-006 — Visible timeouts

Every timeout MUST identify what was awaited, how long it waited, and what the user can do next.

### AC-DOCTOR-001 — Comprehensive doctor

`jarn doctor` MUST inspect:

- active J.A.R.N. executable and all shadowing candidates;
- version and install method;
- OS, architecture, and libc;
- PATH, shell, and profile state;
- installation-directory permissions and free space;
- uv and managed Python;
- Codex CLI path/version/protocol compatibility;
- auth/account state;
- model-catalog source/freshness;
- selected model and route availability;
- config/schema/migration state;
- keychain and secret-file permissions;
- workspace trust and sandbox capability;
- essential provider/network reachability;
- update channel and artifact metadata.

### AC-DOCTOR-002 — Safe repair

`jarn doctor --fix` MUST modify only safe, scoped, recoverable targets. It MUST show the planned changes and support `--dry-run`.

### AC-DOCTOR-003 — Automation and support report

Provide `jarn doctor --json` and a redacted `jarn doctor --report`. The report MUST exclude prompts, file contents, secrets, and tokens and MUST pass automated secret scanning.

## K. Update, rollback, and uninstall

### AC-UPDATE-001 — Unified updater

`jarn update` MUST reuse the supported installation mechanism and detect how the current executable was installed.

### AC-UPDATE-002 — Verify before activation

Updates MUST verify integrity and smoke-test the candidate before replacing the current version.

### AC-UPDATE-003 — Automatic rollback

If activation verification fails, the updater MUST restore the prior working version automatically.

### AC-UPDATE-004 — Check and channels

Provide `jarn update --check`, machine-readable output, and stable/beta channels with stable as default.

### AC-UPDATE-005 — Non-blocking notices

Startup update checks MUST be cached, have a short timeout, and never block normal startup.

### AC-UPDATE-006 — Migration transparency

Breaking changes and config migrations MUST be shown before update. At least one previous working version SHOULD remain available for rollback.

### AC-UPDATE-007 — Explicit rollback

Provide `jarn rollback` or an equivalent supported command that identifies the target version.

### AC-UNINSTALL-001 — Itemized uninstall

Uninstall MUST list exact targets and separately ask about executable, exclusively owned dependencies, config, sessions, cache, and credentials.

### AC-UNINSTALL-002 — Shared dependencies protected

Codex CLI, uv, Node, Python, or other shared dependencies MUST not be removed without clear proof of exclusive ownership and explicit confirmation.

## L. Performance and reliability

### AC-PERF-001 — Fast utility commands

On the reference machine with a warm filesystem cache, `jarn --version` and `jarn --help` SHOULD have p95 startup of 500 ms or less.

### AC-PERF-002 — Responsive interactive startup

The local interactive UI SHOULD accept input within p95 two seconds, excluding network authentication.

### AC-PERF-003 — Responsive typing

Normal local keystroke-to-render latency SHOULD remain below 50 ms.

### AC-PERF-004 — Visible network wait

Catalog/auth network waits longer than one second SHOULD show progress and MUST have configurable bounded timeouts.

### AC-PERF-005 — Diagnostic timeout

Offline doctor SHOULD finish in ten seconds. Network checks MUST use per-check timeouts and MUST not hang the whole command.

### AC-REL-001 — Atomic writes

Config, session, checkpoint, and install metadata writes MUST be atomic and durable as appropriate.

### AC-REL-002 — Clean shutdown

Normal exit and cancellation MUST not leave unwanted child processes, locks, or background threads.

### AC-REL-003 — Malformed-stream resilience

Malformed provider output, partial JSON, disconnects, and protocol version mismatches MUST produce controlled errors rather than crash the TUI.

### AC-REL-004 — No duplicated side effects

Retries/reconnects MUST not repeat external tool actions unknowingly. Side-effecting operations require identifiable state or idempotency protection.

## M. Security, privacy, and supply chain

### AC-SEC-001 — Release integrity

Every release artifact MUST have a checksum and verifiable provenance. Signed artifacts and an SBOM SHOULD be provided for GA.

### AC-SEC-002 — Pinned automation

Release/CI dependencies and GitHub Actions MUST follow the repository's pinning and supply-chain policy.

### AC-SEC-003 — HTTPS and mismatch refusal

Installer and updater MUST use HTTPS and refuse checksum/signature mismatches.

### AC-SEC-004 — Telemetry boundary

Prompts, file contents, paths, commands, model output, API keys, auth tokens, and credential metadata MUST NOT be sent as telemetry.

### AC-SEC-005 — Consent and control

If telemetry exists, its state and data categories MUST be documented and controllable through `jarn telemetry status|on|off`. Consent behavior must follow project policy.

### AC-SEC-006 — Central redaction

Logs, errors, events, support bundles, and provider diagnostics MUST use centralized secret redaction.

### AC-SEC-007 — Safe subprocess construction

Model-generated text, paths, URLs, model IDs, and config values MUST not be concatenated into shell commands. Subprocess integrations must use argv and `shell=False` except for explicit permission-gated shell features.

### AC-SEC-008 — Secure temporary files

Temporary paths MUST be created safely, use suitable permissions, and be cleaned on success, error, and signals.

## N. Accessibility and internationalization

### AC-A11Y-001 — Not color-only

Critical status MUST use text/symbols in addition to color.

### AC-A11Y-002 — Color controls

Support `NO_COLOR`, high-contrast presentation, and readable light/dark themes.

### AC-A11Y-003 — Keyboard completion

All essential setup and interactive actions MUST be usable through the keyboard.

### AC-A11Y-004 — Plain-text parity

The plain-text wizard MUST support all critical install/auth/model/recovery choices offered by the full TUI.

### AC-I18N-001 — Locale resilience

Non-UTF-8 locale problems MUST result in a clear remediation rather than a crash. Unicode paths and Thai project/file names MUST be tested.

## O. Documentation and support

### AC-DOC-001 — Five-minute quickstart

README MUST provide one recommended path from install to first prompt that a new user can complete without prerequisite knowledge.

### AC-DOC-002 — One recommended installer

The main documentation MUST present a single recommended installer. Alternative package-manager methods belong in Advanced installation.

### AC-DOC-003 — Required documentation

Document:

- supported platforms;
- installation and first run;
- ChatGPT and API-key authentication;
- local models;
- model/reasoning selection;
- permissions;
- update and rollback;
- uninstall and data retention;
- troubleshooting;
- privacy and security;
- config migration;
- known limitations.

### AC-DOC-004 — Required troubleshooting cases

Include GLIBC mismatch, command shadowing, PATH activation, missing Codex CLI, browserless login, device login, subscription/API billing mismatch, empty/stale catalog, unavailable models, corrupt config, proxy/custom CA, and filesystem permission failures.

### AC-DOC-005 — Error references

Stable error codes SHOULD link to versioned troubleshooting pages, but the terminal message itself MUST remain actionable without the web.

### AC-DOC-006 — Behavior-documentation consistency

Commands and screenshots/examples MUST be validated against the release candidate. Documentation must not claim a success path that the published artifact cannot complete.

## P. Automated test matrix

### TEST-INSTALL

End-to-end tests MUST cover:

- clean machine without Node/Python/uv;
- existing working binary;
- existing broken PyInstaller binary;
- global npm installation;
- pip user, pipx, and uv tool installations;
- `~/.local/bin` absent from PATH;
- hashed old shell command;
- alias or function named `jarn`;
- libc older than build environment;
- musl behavior or explicit unsupported response;
- ARM64;
- interrupted download;
- checksum mismatch;
- disk full/read-only home;
- proxy and network timeout;
- no interactive TTY;
- SSH/headless setup;
- same-version rerun;
- upgrade and rollback.

### TEST-AUTH

Contract/integration tests MUST cover:

- missing and outdated Codex CLI;
- compatible existing login;
- browser success and callback failure;
- device success, timeout, expiry, and cancellation;
- login subprocess non-zero;
- login subprocess zero but account signed out;
- API-key mode while subscription is selected;
- expired/revoked credentials;
- refresh failure;
- workspace denial;
- verification timeout;
- malformed app-server response;
- preservation of visible URL/code/output.

### TEST-MODEL

Tests MUST cover:

- one-page and paginated `model/list`;
- empty catalog;
- hidden models;
- account-specific availability;
- provider default;
- reasoning-effort metadata;
- deprecated/renamed model;
- malformed response;
- network timeout;
- fresh/stale/missing cache;
- offline static fallback labeling;
- manual model validation;
- selected model removed after update;
- identical setup and `/model` catalog behavior.

### TEST-CONFIG

Tests MUST cover migrations from supported prior releases, custom-key preservation, corrupt input, interrupted migration, rollback, backup, permissions, and concurrent access.

### TEST-UI

Tests MUST cover 80x24, resize, `NO_COLOR`, `TERM=dumb`, Thai input, multiline paste, `Ctrl+C`, back/cancel/resume, unknown slash commands, and ANSI-free JSON output.

### TEST-SECURITY

Tests MUST cover secret redaction, path traversal, symlink escape, shell injection, permission bypass, MCP/provider bridge bypass, over-broad persisted approvals, temporary-file permissions, malicious model/path/URL values, and secret scanning of support bundles.

### TEST-RELEASE

Release tests MUST fetch published artifacts and verify checksum, executable startup, oldest supported libc, clean install, upgrade from at least two previous releases, rollback, uninstall, and reinstall with preserved user data.

Public CI MUST use fake/contract Codex app-server fixtures without credentials. Live auth and entitlement tests may use a protected private/manual canary, and credentials must never enter logs or artifacts.

## Q. User acceptance tests

### UAT-001 — New Ubuntu SSH user

- Run one installation line.
- Install no development runtime manually.
- Choose Continue with ChatGPT.
- Install or reuse Codex CLI.
- See device URL/code.
- Complete login and verify account/plan.
- Select a live-supported default automatically.
- Start the first prompt without PATH/config edits.
- Observe no false success.

### UAT-002 — Legacy npm collision

- Begin with old global npm J.A.R.N.
- Detect all executable locations.
- Ensure the post-install command invokes the new version.
- Present safe optional cleanup.
- Preserve and migrate config with backup.

### UAT-003 — macOS desktop

- Browser opens automatically while fallback URL remains visible.
- Callback returns to CLI.
- Account and model are verified.
- J.A.R.N. is immediately usable.

### UAT-004 — OpenCode Go API user

- Key does not echo or enter config/logs.
- Keychain/environment reference works.
- Provider model choices are current or honestly cached.
- Any billable validation is disclosed first.

### UAT-005 — Local Ollama user

- Endpoint and models are discovered.
- No API-key prompt appears.
- Cloud outage does not block local setup.
- Unavailable local model receives clear remediation.

### UAT-006 — Network failure

- Prior installation and config remain intact.
- The failed stage is identified.
- Retry is available.
- No `Done` is printed.
- Doctor can diagnose the resulting state.

Each UAT records manual commands, decisions, errors, time-to-ready, and documentation lookups. The standard path targets one initial install command, no manual config/PATH editing, and no external documentation lookup.

## R. Release quality gates

A release MUST NOT ship until:

- all MUST/P0 acceptance criteria pass;
- Tier-1 platform matrix passes;
- no known Severity-1 or Severity-2 issue remains;
- clean install from the published URL passes;
- upgrades from at least two prior releases pass;
- rollback and uninstall pass;
- the Ubuntu glibc 2.35 plus npm-shadow regression passes;
- every auth failure path avoids false success;
- model selection uses the unified live catalog;
- documentation matches actual behavior;
- checksums and required provenance exist;
- lint, type checking, tests, packaging, and security checks pass;
- GitHub Actions passes without unjustified skips;
- a release evidence report is complete.

## S. Readiness metrics

Targets before GA:

- 100% supported automated install scenarios pass;
- zero known false-success paths;
- zero silent authentication failures;
- zero manual PATH edits in standard UAT;
- zero manual model-ID entry in standard UAT;
- every model list is live/cached/fallback with visible provenance;
- every P0 user-facing error has actionable remediation;
- 100% successful standard-path UAT runs;
- 100% successful tested upgrade and rollback paths;
- zero prompt/secret leakage in logs or support bundles.

If privacy-preserving telemetry is enabled, permissible aggregate categories may include platform/version, install-stage result, setup completion stage, auth mechanism category, catalog live/cache/fallback result, time-to-ready, crash count, and stable error code. User content and identifying local data remain prohibited.

## T. Implementation constraints

- Preserve backward compatibility where practical.
- Keep aliases and deprecation notices when commands change.
- Do not remove advanced providers/features merely to simplify first run.
- Use one auth-status service and one model-catalog service across all interfaces.
- Use atomic writes and recoverable activation.
- Construct subprocesses with argv, not concatenated shell strings.
- Centralize secret redaction.
- Add regression coverage with each fix.
- Do not make CI green by weakening assertions or coverage.
- Do not infer model entitlement from plan names or documentation.
- Do not infer authentication success from process exit code.
- Do not infer installation success from an absolute-path smoke test alone.
- Do not require `sudo` in the standard path.
- Do not destructively clean old installs without explicit confirmation.
- Preserve unrelated user changes in a dirty worktree.
- Do not publish/tag a release until required verification passes and publishing is authorized.

## U. Recommended implementation milestones

### Milestone 0 — Truthful installation

- Fix executable shadowing and active resolution.
- Handle parent-shell activation honestly.
- Make install/update transactional with rollback.
- Add the exact real-world regression tests.

### Milestone 1 — Truthful onboarding and authentication

- Stop suppressing material auth errors.
- Detect/install/validate Codex CLI.
- Select browser versus device flow.
- Verify account after login.
- Implement incomplete/resumable setup state.

### Milestone 2 — Dynamic model experience

- Build unified ModelCatalog.
- Connect paginated Codex `model/list`.
- Add model-specific reasoning effort.
- Add cache provenance and offline behavior.
- Use the catalog in setup, `/model`, doctor, routing, and pre-turn validation.

### Milestone 3 — General-user polish

- Simplify the provider screen.
- Implement doctor/repair.
- Add stable error codes.
- Complete update/rollback/uninstall.
- Complete plain-text/accessibility support.
- Align documentation and help.

### Milestone 4 — GA validation

- Run the full platform matrix.
- Test published artifacts.
- Test upgrades/config migrations.
- Run security and performance checks.
- Complete manual UAT.
- Produce the release evidence report.

## V. Required deliverables

1. Implementation for all P0 criteria.
2. Unit, integration, protocol-contract, security, and E2E tests.
3. Corrected `install.sh`.
4. Native Windows installer or an explicit early WSL-only contract.
5. Unified authentication service.
6. Unified model-catalog service.
7. Doctor and safe repair functionality.
8. Update, rollback, and uninstall flows.
9. Versioned config migrations and backups.
10. README, quickstart, troubleshooting, support matrix, and security/privacy documentation.
11. Release artifact verification and compatibility workflow.
12. Reproducible UAT scripts.
13. Release evidence report.

The evidence report MUST map:

| Field | Required evidence |
|---|---|
| Criterion | Stable criterion ID |
| Status | Passed, failed, or blocked |
| Implementation | File/module and concise description |
| Automated test | Test name/path |
| Platform | OS, version, architecture, libc when relevant |
| Command | Reproducible command |
| Result | Concise observed output/result |
| Limitation | Any remaining constraint |

## W. Completion rule

The implementation goal MUST NOT be marked complete until all of the following are true:

- every P0/MUST item is implemented and verified;
- the reported Ubuntu/npm/GLIBC/auth/model regression passes end to end;
- the exact published curl command installs successfully;
- login visibly presents URL/code and verifies the real account state;
- the model picker uses the live account/provider catalog;
- the standard user reaches the first prompt without manual PATH/config work;
- local validation and CI pass;
- published-artifact canaries pass;
- no required work remains;
- the evidence report and known limitations are delivered.

If blocked, exhaust safe in-scope alternatives and report the concrete repeated blocker. Difficulty, time, incomplete implementation, or a nearly exhausted budget are not completion conditions. A degraded or partially configured state must never be labeled successful.
