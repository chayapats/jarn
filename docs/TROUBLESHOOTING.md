# Troubleshooting

Start with the offline, non-mutating diagnostic:

```bash
jarn doctor
jarn doctor --json
jarn doctor --fix --dry-run
```

Use `jarn doctor --report jarn-support.json` when sharing a report. It is an
allowlisted, redacted file written with mode `0600`; review it before sending.

## GLIBC mismatch

Typical message:

```text
GLIBC_2.38 not found (required by libpython3.12.so.1.0)
```

This means the downloaded executable was built against a newer libc than the host.
Do **not** replace the operating system's libc manually. The official installer
smoke-tests a staged binary and, on a supported Ubuntu/Debian host, switches to an
isolated managed-Python build of the same J.A.R.N. version. If both paths fail, the
old executable remains active and the failed stage is reported.

Collect these facts:

```bash
getconf GNU_LIBC_VERSION
jarn doctor --json
```

glibc 2.31 or newer on a documented Ubuntu/Debian release is supported. musl/Alpine
is explicitly unsupported. See [Supported platforms](SUPPORTED_PLATFORMS.md).

## Command shadowing or the old version still runs

Multiple npm, pip, uv, or old binary installations can all provide `jarn`. A healthy
file at `~/.local/bin/jarn` is not enough if the shell resolves another path first.

```bash
type -a jarn
command -V jarn
jarn --version
jarn doctor --json
```

The installer inventories every visible executable plus aliases/functions and
verifies login/interactive shell resolution. It never deletes an old installation
automatically. After a verified install that exits `10`, replace the parent shell
with the exact activation command printed by the installer. For a shell command
cache, `hash -r` (bash) or `rehash` (zsh) may be useful, but a new login shell is the
authoritative check.

If an alias or function owns the name, inspect the shell startup file that defines
it and remove it only after confirming it is obsolete. Then open a fresh login shell
and rerun all four commands above.

## `~/.local/bin` is not on PATH

The installer updates a supported shell profile atomically and verifies the result.
It cannot change the environment of its already-running parent process. Exit `10`
therefore means “installed and verified, activation still required,” not success.
Run the printed `exec "$SHELL" -l` action or open a new login terminal. You should not
need to hand-edit PATH in the standard flow.

If a new login shell still fails, `jarn doctor --json` reports PATH, profile, command
resolution, install record, and directory permissions without executing a repair.

## Missing or outdated Codex CLI

ChatGPT subscription mode requires a compatible official standalone Codex CLI.

```bash
jarn auth status
jarn auth repair
```

Setup and repair disclose source, version, and destination before installing or
updating it in user space. The candidate is downloaded, integrity-checked,
smoke-tested, activated atomically, and app-server handshaken. Declining the offer
leaves other providers available but ChatGPT setup incomplete. A `codex --version`
exit of zero alone is not sufficient; protocol compatibility must also pass.

Do not install a similarly named third-party package or use `sudo npm install` as a
repair. If an old Codex command shadows the managed candidate, follow the command
shadowing section above.

## Browserless, SSH, container, or remote login

J.A.R.N. auto-selects device login when no usable local browser/display is detected:

```bash
jarn auth login --device
```

Keep the terminal open. Copy the visible verification URL and one-time code to a
browser on another device. The CLI continues to show progress and expiry. Timeout,
expiry, cancellation, callback failure, or network failure returns nonzero and does
not commit a “ready” setup state.

On a desktop you can force browser flow with `jarn auth login --browser`. The URL is
still printed before any open attempt so popup/browser failure cannot hide it.

Each auth wait defaults to 120 seconds. On a slow link, choose a finite deadline
explicitly with `jarn auth login --device --timeout 300`. To apply the same deadline
to first-run setup, set `JARN_AUTH_TIMEOUT_SECONDS=300`; effective values are always
bounded to 1–900 seconds. A timeout message states what Codex was waiting for and the
elapsed bound, and setup remains incomplete rather than reporting success.

## Login command says success but account is signed out

J.A.R.N. does not trust subprocess exit zero. Completion requires the app-server
login-completed/account-updated event followed by a refreshed `account/read` that
verifies managed ChatGPT mode and usable account state.

```bash
jarn auth status --refresh --json
jarn auth repair
```

If status remains signed out, expired, revoked, refresh-failed, or workspace-denied,
follow the exact action in its structured error. Setup remains resumable and prior
configuration stays intact.

## ChatGPT subscription versus API billing mismatch

If Codex reports API-key mode while the selected profile is ChatGPT subscription,
J.A.R.N. refuses readiness. API-key authentication can be valid but it is a different
billing route.

- To use the subscription, run `jarn auth login` and verify `jarn auth status` says
  managed ChatGPT.
- To use API billing, select an API-key provider/profile in Advanced setup instead.

J.A.R.N. never infers entitlement from a plan name and never silently switches a
billable route.

## Empty or stale model catalog

Open `/model` and choose refresh. Each list labels its provenance as live, cache,
stale cache, local discovery, or offline fallback. A static fallback is navigation
help only and is marked “availability unverified.”

For ChatGPT, status must be ready before the paginated `model/list` request can
represent account availability. Check:

```bash
jarn auth status
jarn doctor --network --json
```

A genuinely empty live result remains empty; J.A.R.N. does not disguise it with
static models. Check account/workspace access and provider service status.

## Selected model became unavailable

Models can be retired, renamed, hidden, or removed from an account. Before a turn,
J.A.R.N. validates the selected model and reasoning effort against the unified
catalog. Refresh `/model`, select an offered replacement/default, and choose only a
reasoning level advertised for that model. Routing targets are validated by the same
service rather than accepted as free-form IDs.

For an offline local model, start Ollama/LM Studio, verify the configured endpoint,
and refresh. Cloud failure must not block a healthy local endpoint.

## Corrupt or unreadable configuration

```bash
jarn config path
jarn config validate
jarn doctor --fix --dry-run
```

Corrupt YAML is never overwritten automatically. Review the source and timestamped
`config.yaml.bak.*` files reported by doctor. Safe migrations create a byte-for-byte
backup, publish atomically, validate after activation, and roll back on failure. A
newer schema requires upgrading J.A.R.N., not rewriting the file with an older copy.

See [Configuration migration](CONFIG_MIGRATION.md).

## Proxy, custom CA, TLS, or restricted network

`curl`, the updater, Codex login, and providers must reach their documented HTTPS
endpoints. Configure the standard environment accepted by your network tooling, for
example `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, and the CA variable required by the
specific HTTP runtime. Do not disable certificate verification.

Run `jarn doctor` offline first, then opt into `jarn doctor --network` so the report
can distinguish DNS, timeout, TLS/CA, proxy, authentication, and provider failures.
Credentials and proxy URLs containing secrets are redacted. A network failure must
not alter the prior executable or configuration, and the installer/updater must not
print `Done`.

## Filesystem permission, read-only home, or no disk space

The standard path is user-space and should not use `sudo`. Confirm the reported
install, state, config, and cache directories belong to your user and are writable.
The installer requires at least 512 MiB temporary free space by default and performs
its checks before activation.

```bash
jarn doctor --json
jarn doctor --fix --dry-run
```

Only apply a proposed repair after reviewing its exact paths. Repairs are allowlisted,
revalidate path/symlink/mode at apply time, and roll back the batch on failure. A
read-only or full filesystem returns nonzero while retaining the previous state.

## Terminal, Unicode, or Thai text problems

Use a UTF-8 locale and terminal. On Linux, `C.UTF-8` is commonly available. If the
terminal is minimal, set `TERM=dumb` for usable plain output; set `NO_COLOR=1` to
disable color. J.A.R.N. must handle Thai combining characters and Unicode file paths
without corrupting input. A non-UTF-8 child-process output error should name the
locale remediation instead of crashing with a raw decoder traceback.

## Still blocked

Capture the exact command, exit code, JARN error code, platform, and whether the
catalog/auth result was live or cached:

```bash
jarn --version
jarn doctor --report jarn-support.json
```

Review the report, then attach it with a concise reproduction. See
[Error codes](ERROR_CODES.md), [Privacy](PRIVACY.md), and
[Known limitations](KNOWN_LIMITATIONS.md).
