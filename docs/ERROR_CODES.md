# Error and exit-code reference (contract version 1)

Every blocking machine-readable failure includes a stable code, summary, cause,
component, retryability, exact next action, and log/report location. Human terminal
output carries the same actionable information. Secret values are centrally
redacted before either form is emitted. Machine output includes
`log_available`; when it is false, human output labels the expected log location
as unavailable and points to `jarn doctor --report FILE` rather than claiming a
file was written.

## Process exit codes

| Exit | Meaning |
|---:|---|
| `0` | Success |
| `1` | Unexpected internal error |
| `2` | Invalid usage or configuration |
| `3` | Authentication or account verification failure |
| `4` | Selected model unavailable or invalid for the current account/provider |
| `5` | Permission denied or approval unavailable |
| `6` | Network or provider failure |
| `7` | Update, activation, or rollback failure |
| `8` | Explicit session budget exceeded |
| `9` | Required verification/output contract failed |
| `124` | Timeout |
| `130` | User cancellation (`Ctrl+C`) |

The installer additionally uses `10` for a verified installation needing parent
shell activation and `20` for a verified installation with incomplete setup. These
are not successful “ready for first prompt” states.

## Stable error families

| Code | Meaning | First action |
|---|---|---|
| `JARN-CONFIG-001` | Invalid/corrupt YAML | Keep the file unchanged; inspect a backup or validate the YAML |
| `JARN-CONFIG-002` | Values do not match the schema | Run `jarn config validate` |
| `JARN-CONFIG-003` | Configuration is from a newer unsupported schema | Upgrade J.A.R.N.; do not rewrite it with the older version |
| `JARN-CONFIG-004` | Migration failed | Inspect the timestamped backup and doctor report |
| `JARN-CONFIG-005` | Safe configuration write failed | Correct ownership/free space and retry |
| `JARN-DOCTOR-001` | A diagnostic check failed | Retry `jarn doctor`; use `--report` for support |
| `JARN-DOCTOR-002` | A requested repair was outside the safe allowlist | Review and fix that item manually |
| `JARN-DOCTOR-003` | Support report could not be safely created | Correct destination permissions and retry |
| `JARN-AUTH-001` | Required Codex dependency/authentication is missing | Run `jarn auth status`, then `jarn auth repair` |
| `JARN-AUTH-002` | Codex dependency is incompatible/outdated | Run `jarn auth repair` and verify the reported version |
| `JARN-AUTH-003` | Account is signed out | Run `jarn auth login` (use `--device` when browserless) |
| `JARN-AUTH-004` | API-key billing mode cannot satisfy ChatGPT subscription auth | Sign in with a ChatGPT account, not API-key mode |
| `JARN-AUTH-005` | Account lacks workspace access | Select/obtain access to the required workspace and retry |
| `JARN-AUTH-006` | Credential is expired or revoked | Sign in again and verify refreshed account state |
| `JARN-AUTH-007` | Credential refresh failed | Check account/network state, then sign in again |
| `JARN-AUTH-008` | Authentication network is unavailable | Check proxy/CA/network, then retry the bounded operation |
| `JARN-AUTH-009` | Codex app-server protocol response is invalid/incompatible | Repair/update Codex and rerun status |
| `JARN-AUTH-010` | Verified login ceremony did not complete | Follow the displayed browser/device recovery action |
| `JARN-MODEL-001` | Selected model is unavailable or invalid | Refresh the catalog and select an available entry |
| `JARN-MODEL-002` | A verified model catalog is unavailable | Restore credentials/network or use an honestly labelled cache |
| `JARN-GATEWAY-001` | Telegram optional dependency is missing | Install the documented Telegram extra for this installation method |
| `JARN-GATEWAY-002` | Gateway configuration is missing or invalid | Validate the global-only gateway configuration and retry |
| `JARN-GATEWAY-003` | Gateway bot credential is missing/unresolvable | Store the token as a supported secret reference and retry |
| `JARN-GATEWAY-004` | Telegram operator allowlist is invalid | Configure one or more numeric allowed user IDs |
| `JARN-GATEWAY-005` | Gateway runtime failed/stopped unexpectedly | Inspect the redacted cause/log, correct the process/backend issue, and restart |
| `JARN-SAFE-001` | Permission policy denied the action | Review target/scope and explicitly approve only if intended |
| `JARN-SAFE-002` | An undeclared tool was classified fail-closed | Inspect the redacted diagnostic, then register and classify the tool before relying on it |
| `JARN-TELEMETRY-001` | The local telemetry sink is corrupt or unreadable | Run `jarn telemetry off`, preserve the sink if needed, then correct permissions or move only that file before opting in again |
| `JARN-TELEMETRY-002` | A partial final telemetry record was repaired and no corruption remains | Review the recovery detail; this warning returns success because the current sink is healthy |
| `JARN-NET-001` | Network/provider request failed | Check network, proxy, CA, and provider status |
| `JARN-CLI-001` | Invalid command usage | Correct the command using `jarn --help` |
| `JARN-CLI-002` | Operation cancelled | Retry only when ready |
| `JARN-UPDATE-001` | Update/rollback failed | Inspect the reported stage; the prior version should remain active |
| `JARN-UPDATE-002` | Install receipt is malformed/missing fields | Repair the receipt with the official installer before changing versions |
| `JARN-UPDATE-003` | Install receipt/path is unsafe or outside the managed layout | Refuse the action; run doctor and repair with the official installer |
| `JARN-UPDATE-004` | No managed install receipt exists | Use the detected package manager or migrate explicitly to the curl installer |
| `JARN-UPDATE-005` | Install receipt cannot be read/locked/verified | Correct state-directory ownership/filesystem support and retry |
| `JARN-UPDATE-006` | Update channel is invalid | Choose `stable` or `beta` |
| `JARN-UPDATE-007` | Release lookup failed or timed out | Check network/proxy/CA and retry |
| `JARN-UPDATE-008` | Release service returned no valid candidate | Retry later or select an exact published version |
| `JARN-UPDATE-009` | Current/running version is invalid | Repair the active installation before updating |
| `JARN-UPDATE-010` | Release artifact download failed/timed out | Check network/proxy/CA and retry; nothing was activated |
| `JARN-UPDATE-011` | Installer checksum/format/encoding verification failed | Do not run it; retry from the canonical release |
| `JARN-UPDATE-012` | POSIX `sh` required by the supported updater is unavailable | Install/use a supported shell or platform installer |
| `JARN-UPDATE-013` | Updater process could not start | Correct executable/permission state and retry |
| `JARN-UPDATE-014` | Installer returned incomplete/failure state | Follow its named stage and recovery command; no success was recorded |
| `JARN-UPDATE-015` | No retained prior version exists | Reinstall the desired version explicitly; rollback cannot proceed |
| `JARN-UPDATE-016` | Rollback layout/canary source configuration is unsafe | Use same-filesystem managed paths and production source defaults |
| `JARN-UPDATE-017` | Retained rollback candidate failed smoke verification | Keep the current version; reinstall a verified candidate |
| `JARN-UPDATE-018` | Rollback staging path already exists | Inspect/remove only the proven stale managed staging entry, then retry |
| `JARN-UPDATE-019` | Rollback activation failed | Current version remains/restores active; correct filesystem failure and retry |
| `JARN-UPDATE-020` | Rollback verification and automatic restoration both failed | Stop and repair from the retained candidates/report before continuing |
| `JARN-UPDATE-021` | Activated rollback candidate failed verification | Automatic restoration was attempted; repair/reinstall explicitly |
| `JARN-UPDATE-022` | Rollback metadata and automatic restoration both failed | Stop and reconcile executable/receipt using doctor/report |
| `JARN-UPDATE-023` | Rollback metadata commit failed | Prior executable state was restored; fix state storage and retry |
| `JARN-UPDATE-024` | Requested version is invalid or ambiguous/missing | Use one exact published semantic version |
| `JARN-UPDATE-025` | Receipt, executable, and version do not agree | Repair the actionable install record before update/rollback |
| `JARN-UPDATE-026` | Config-migration preview could not be generated | Fix/restore config first; update was not activated |
| `JARN-BUDGET-001` | Explicit session budget exceeded | Increase the explicit cap or reduce the task |
| `JARN-VERIFY-001` | Required verification/output contract failed | Inspect the verification result and fix the reported failure |
| `JARN-I18N-001` | Child output cannot be decoded under the active locale | Configure UTF-8 (for example `C.UTF-8`) or fix the command's output |
| `JARN-INTERNAL-001` | Unexpected internal failure | Run `jarn doctor --report FILE` and inspect the privacy-scanned report and local log |
| `JARN-INSTALL-001` | Installer preflight, download, verification, or transaction failed | Correct the reported cause and rerun the safe installer; the prior command/data should remain |
| `JARN-UNINSTALL-001` | Invalid uninstall category selection | Select only documented itemized categories |
| `JARN-UNINSTALL-002` | One or more selected items could not be removed | Correct the reported permission/backend issue and retry the same categories |
| `JARN-UNINSTALL-003` | Unsafe/tampered install receipt | Refuse removal; repair metadata with doctor/the official installer |

Numbers and meanings are compatibility API. New detail may be added to JSON, but an
existing code is not silently reused. Automation should branch primarily on the
process exit category and log the stable JARN code for diagnosis.

See [Troubleshooting](TROUBLESHOOTING.md) for scenario-specific recovery. The error
message itself is designed to remain sufficient when no browser is available.
