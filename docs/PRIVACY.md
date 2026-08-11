# Privacy and data boundaries

J.A.R.N. is a local terminal application, but a configured language-model provider
receives the context needed to answer a prompt. This page separates that expected
provider traffic from logs, telemetry, tracing, credentials, and support reports.

## What can leave the machine

- Prompts, selected project context, model messages, and tool results may be sent to
  the model provider selected in the active route. Review that provider's retention
  and training policy.
- Web search, MCP, hooks, and explicit network tools contact the endpoints shown in
  configuration or an approval prompt. The permission engine and network policy
  still apply.
- ChatGPT subscription authentication is owned by the official Codex CLI. J.A.R.N.
  displays the login challenge and asks Codex app-server to verify the account; it
  does not read or copy the OAuth token.
- API keys are read from environment variables, the OS keychain, or J.A.R.N.'s
  permission-restricted secret store. Configuration contains references, not raw
  secret values.

## Local telemetry

Telemetry is **off by default**, local-only, and has no upload sink. When explicitly
enabled, `~/.jarn/telemetry.jsonl` contains event names, timestamps, a random local
installation identifier, and numeric/boolean measurements. Its recorder discards
string-valued properties.

It must never contain prompts, model output, file contents, paths, commands, model
IDs, API keys, auth tokens, account/workspace metadata, or credential metadata.

```bash
jarn telemetry status
jarn telemetry on
jarn telemetry off
```

`jarn telemetry status` returns non-zero with `JARN-TELEMETRY-001` and
`ok: false` when the local sink remains corrupt or cannot be read; it never labels
that state successful. A repaired, crash-truncated final record is reported
separately as `JARN-TELEMETRY-002`, `health: recovered`, and remains a successful
status because no corruption is left.

Turning telemetry off stops new records; it does not silently delete existing local
records. Use the itemized uninstall flow to remove the cache/telemetry category, or
inspect and delete the reported local path yourself.

## Tracing is separate

LangSmith and OpenTelemetry tracing are separate, advanced, opt-in observability
features. Depending on upstream instrumentation and exporter configuration, traces
can contain prompts, model messages, tool names, and results and can be sent to a
remote service. Do not enable remote tracing for sensitive work until you have
reviewed the exporter and destination. `jarn telemetry off` does not disable a
tracer that you enabled separately.

## Local records

By default J.A.R.N. may store configuration, session/checkpoint data, redacted JSONL
transcripts, rotating logs, caches, and credentials or credential references under
its global data directories. Project-local `.jarn/` files remain part of the project
and are never removed by global uninstall.

The central redactor is applied to logs, user-facing errors, transcripts, provider
diagnostics, and support reports. Redaction is defense in depth, not permission to
place secrets in prompts or source files.

## Support reports

`jarn doctor --report FILE` and `jarn bug` build the same strict allowlisted JSON
report, scan it for secret patterns and local paths, and write it atomically with
mode `0600`. They exclude prompts, file contents, command history, raw logs, tokens,
raw environment variables, and credential values. Always review a report before
sharing it.

`jarn bug` keeps that report local at `~/.jarn/bug-report.json`. Without
`--dry-run`, it shows the data-category preview and asks before opening GitHub. The
pre-filled URL contains only a fixed issue template and the J.A.R.N. version; report
content is never placed in the URL automatically. Attaching the reviewed local file
is a separate user action.

## Deletion and retention

`jarn uninstall` asks independently about executable, isolated dependencies,
configuration, sessions, cache/telemetry, and credentials. Only the executable is
selected by default; user data is retained unless chosen explicitly. Codex-managed
ChatGPT credentials and shared Node/Python/uv/Codex installations are outside this
uninstall boundary.

See [Security](../SECURITY.md) for the execution threat model and
[Configuration](CONFIGURATION.md) for observability settings.
