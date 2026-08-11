# Update, rollback, and uninstall

The official installer and updater share one transactional activation path. A new
candidate is staged, integrity-checked, smoke-tested, activated atomically, and
tested again through the command users will actually resolve. The last verified
version is retained for rollback.

## Check and update

```bash
jarn update --check
jarn update --check --json
jarn update --channel stable
jarn update --channel beta --dry-run
```

Stable excludes prereleases; beta may select them. JSON output has a versioned
schema and no installer progress prose. Updates use HTTPS, verify release integrity,
and run the installer for one exact version. They never remove the active executable
until the candidate passes its pre-activation smoke test.

Before downloading or activating anything, `jarn update` now prints an update
preview containing:

- the installed version and selected target (`current -> target`);
- the validated installation owner and the action it permits;
- bounded, secret-redacted notes from the primary GitHub release record;
- declared breaking changes, or the precise status **none declared** (which is
  not a promise that none exist);
- the current and target configuration schema, every known migration step, and
  the timestamped sibling-backup pattern used before migrated configuration is
  activated;
- whether a retained rollback candidate already exists and the retention policy
  for the current executable.

`--dry-run` builds the same preview and sends `--dry-run` to the verified
installer, so it does not activate a candidate, edit shell profiles, migrate
configuration, or create a backup. `--json` returns the preview under `preview`
inside the single versioned JSON document. Release notes are capped at 4,000
characters, terminal control characters are removed, and recognised credentials
are redacted. Release metadata can declare its target schema with
`JARN-CONFIG-SCHEMA: <integer>`; if absent, the preview clearly marks the target
as inferred from the updater's currently supported schema.

## Installation ownership

An update is allowed only after the install receipt passes actionable path
validation and the recorded version matches the active command's `--version`.
The updater never treats a JSON file alone as proof that it owns an executable.

| Detected owner | Detection evidence | `jarn update` behaviour |
|---|---|---|
| Official curl installer (`binary`) | Validated receipt and matching executable | Runs the verified installer with `--method binary` |
| Official curl installer (`python`) | Validated receipt and matching executable | Runs the verified installer with `--method python` |
| uv tool | Validated method or uv tool path | Refuses ownership change; shows `uv tool install --force jarn==<target>` |
| pipx | Validated method or pipx environment path | Refuses ownership change; shows `pipx install --force jarn==<target>` |
| pip / pip-user | Validated method, site-packages path, or bounded Python console-script signature | Refuses ownership change; shows `python3 -m pip install --upgrade jarn==<target>` |
| npm | Validated method, node_modules path, or bounded Node console-script signature | Refuses ownership change; shows `npm install --global jarn-cli@<target>` |
| Homebrew | Validated method or resolved Cellar/Homebrew path | Refuses ownership change; shows `brew upgrade jarn` |
| Unknown, ambiguous, or invalid receipt | No trustworthy ownership proof | Refuses before download or execution |

Package-manager commands are recommendations displayed for review; `jarn update`
does not silently execute a shared manager or replace its files with curl-managed
files. To migrate ownership, first review `jarn update --dry-run`, then invoke the
official curl installer separately. That separate command is the explicit
confirmation boundary. A present but malformed, unsafe, or version-mismatched
receipt must be repaired with `jarn doctor --report` or the official installer;
path heuristics never override a bad receipt.

Exit `0` means the selected command is healthy and active. Exit `10` means the new
installation is healthy but the invoking parent shell must be replaced before
resolution can be trusted. Exit `20` means installation is healthy but first-time
setup remains incomplete. Other nonzero exits identify a failed stage and preserve
or restore the prior executable.

Configuration is not rewritten by the updater itself. If the new executable needs
a supported schema migration, the normal configuration loader performs it
transactionally on first use: validate the source, create a timestamped
byte-for-byte sibling backup, validate the candidate, publish atomically, and
restore on verification failure. A corrupt, invalid, future-version, or downgrade-
incompatible configuration is reported as requiring recovery review rather than
being silently rewritten.

## Roll back

```bash
jarn rollback
jarn rollback --json
```

Rollback is available when the installer retained a prior verified version. It
smoke-tests that candidate, atomically exchanges it with the active executable,
smoke-tests the newly active command, and commits the install record. Any failure
attempts to restore the original active version. The version rolled away from is
retained, so another explicit rollback can move forward again.

Rollback changes executable/runtime state, not user configuration or sessions. If a
new release migrated configuration, follow [Configuration migration](CONFIG_MIGRATION.md)
and restore a reviewed backup only when the older executable requires it.

## Uninstall and data retention

Run `jarn uninstall` for an inventory and independent choices:

- executable and retained rollback binary;
- dependencies owned exclusively by J.A.R.N.;
- global configuration and trust records;
- sessions, checkpoints, transcripts, memory, and wiki;
- caches, logs, and local telemetry;
- J.A.R.N.-managed credentials.

Only the executable category defaults to removal. Configuration, sessions, cache,
and credentials default to **keep**. `jarn uninstall --yes` therefore removes only
the executable category. Automation that intends broader removal must name every
category explicitly (for example `--config --sessions --cache --credentials`) and
add `--yes`; there is no implicit “delete all data” form.

All interactive decisions are collected before deletion starts. `Ctrl+C`, EOF or
terminal closure at any category prompt discards every earlier “yes”, removes
nothing, emits `JARN-CLI-002`, and exits `130`. Declining every category has the
same explicit cancellation result rather than being reported as an internal
failure.

Shared Codex CLI, uv, Node.js, and Python installations are preserved. Codex-owned
ChatGPT login is not removed. Project-local `.jarn/` directories are never touched.
Any removal failure is reported and produces a nonzero exit; “completed” is not
printed when selected material remains.

## Recovery

If an update fails, run:

```bash
jarn doctor
jarn doctor --report jarn-support.json
jarn rollback
```

Do not delete the retained version or install record before diagnosis. For command
shadowing or a shell still resolving an old copy, use the exact steps in
[Troubleshooting](TROUBLESHOOTING.md#command-shadowing-or-the-old-version-still-runs).
