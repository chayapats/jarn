# ChatGPT authentication status contract (schema version 1)

`jarn auth status --json` emits exactly one UTF-8 JSON object followed by a newline.
It never emits OAuth tokens, cookies, API keys, raw account IDs, or terminal login
challenges. `jarn codex status --json` is a compatibility alias with the same contract.

```json
{
  "schema_version": 1,
  "provider": "codex_subscription",
  "state": "authenticated_chatgpt",
  "authenticated": true,
  "ready": true,
  "auth_mode": "chatgpt",
  "plan_type": "plus",
  "workspace": {"id_hash": "0123456789abcdef", "name": null},
  "dependency": {
    "state": "compatible",
    "executable": "/home/user/.codex/packages/standalone/current/bin/codex",
    "version": "0.147.0",
    "minimum_version": "0.100.0",
    "detail": null
  },
  "checked_at": "2026-08-09T12:00:00Z",
  "error": null
}
```

## Fields

| Field | Type/nullability | Meaning |
|---|---|---|
| `schema_version` | integer, always `1` | Version of this JSON contract, independent of config schema |
| `provider` | string | Always `codex_subscription` in version 1 |
| `state` | enum string | Classified dependency/account/refresh state listed below |
| `authenticated` | boolean | Codex reported an authenticated account of any billing mode |
| `ready` | boolean | The account is verified specifically for ChatGPT subscription use |
| `auth_mode` | string or `null` | `chatgpt`, `api_key`, `signed_out`, or a redacted unknown protocol value |
| `plan_type` | string or `null` | Non-secret plan label exposed by Codex; not entitlement proof |
| `workspace` | object or `null` | Optional workspace name and a one-way, truncated `id_hash`; never a raw ID |
| `dependency` | object | Bounded Codex executable/version compatibility probe |
| `checked_at` | RFC 3339 UTC string | Time this status was verified |
| `error` | object or `null` | Stable actionable error anatomy when `ready` is false |

`workspace.id_hash` and `workspace.name` are independently nullable. Dependency fields
`executable`, `version`, `minimum_version`, and `detail` are nullable because a missing
or malformed executable cannot provide every fact.

The `error` object contains `code`, `message`/`summary`, `cause`, `component`,
`retryable`, `recovery`/`action`, and `log_path`. Consumers may ignore unknown future
fields. Existing version-1 fields and enum meanings are not silently repurposed.

## State enums

Authentication `state` is one of:

- `dependency_missing`
- `dependency_incompatible`
- `signed_out`
- `login_pending`
- `authenticated_chatgpt`
- `authenticated_api_key`
- `expired_or_revoked`
- `workspace_denied`
- `refresh_failed`
- `network_unavailable`
- `unknown_protocol_error`

`dependency.state` is one of `missing`, `available_unverified`, `compatible`, or
`incompatible`. A successful `codex --version` probe alone is
`available_unverified`; only an app-server handshake promotes it to `compatible`.

## Exit behavior

| Command result | Exit |
|---|---:|
| `status` or `repair` returns `ready: true` | `0` |
| Missing/outdated dependency, signed out, API-key billing mismatch, expired token, denied workspace, refresh/network/protocol/login failure | `3` |
| Login cancelled with `Ctrl+C` | `130` |

`jarn auth login --json` and `repair --json` are JSONL ceremonies: they may emit a
dependency offer/result, a visible login-challenge record, and `auth_progress` records
before the final status object. Device challenges contain the URL, one-time user code, expiry, and cancel hint
because the user must see them; callers must display them but must not persist raw
terminal output. Completion is gated on a refreshed `ready: true` status, not a child
process exit code.

Every Codex authentication wait is bounded. Use `--timeout SECONDS` on any
`jarn auth`/`jarn codex` action, or set `JARN_AUTH_TIMEOUT_SECONDS` for setup and all
auth commands. The default is 120 seconds; accepted effective values are clamped to
1–900 seconds, and invalid environment values fall back to the default. Human login
shows the URL/code before waiting and announces the account-verification stage.

See [Troubleshooting](TROUBLESHOOTING.md#chatgpt-login-has-no-visible-url-or-code) and
[Error codes](ERROR_CODES.md).
