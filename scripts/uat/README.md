# Reproducible GA UAT harnesses

All harnesses are local dry-runs by default. Dry-run mode does not invoke
`ssh`; target mutation requires both `--execute` and typing the exact disposable
host. Run `--help` on a harness for its fixture contract.

| Harness | Fixture | Mutating operation |
|---|---|---|
| `uat-001-ubuntu-ssh.sh` | Fresh Ubuntu SSH account without J.A.R.N. state | Published install, onboarding, and one interactive prompt |
| `uat-002-legacy-collision.sh` | Old global npm command plus config v0-v2 | User-space install and product config migration check; old npm path is retained |
| `uat-003-macos-desktop.sh` | Fresh macOS 13+ account owning the active GUI console session | Browser/loopback ChatGPT login, live catalog setup, and one prompt; live auth terminal is not captured |
| `uat-004-anthropic.sh` | Fresh config with `ANTHROPIC_API_KEY` pre-provisioned in the login shell | Anthropic setup by environment reference, no-leak scan, and optional disclosed validation |
| `uat-005-ollama.sh` | Fresh config plus non-empty loopback Ollama endpoint | Local setup and turns with cloud proxy blocked, plus deliberately unavailable-model remediation |
| `uat-006-network-failure.sh` | Healthy prior command and config | Release download is redirected to closed loopback port 9; prior hashes must remain unchanged |

The controller needs Bash, SSH, and Python 3 only to orchestrate and write JSON.
The target prerequisites are checked before confirmation. UAT-001 must not have
a development runtime installed manually; the published product path owns any
dependency acquisition it needs.

Example plan-only runs:

```sh
scripts/uat/uat-001-ubuntu-ssh.sh --host USER@HOST
scripts/uat/uat-002-legacy-collision.sh --host USER@HOST
scripts/uat/uat-003-macos-desktop.sh --host USER@MAC
scripts/uat/uat-004-anthropic.sh --host USER@HOST
scripts/uat/uat-005-ollama.sh --host USER@HOST
scripts/uat/uat-006-network-failure.sh --host USER@HOST
```

Example protected result destination:

```sh
scripts/uat/uat-006-network-failure.sh \
  --host USER@HOST \
  --output artifacts/ga-evidence/uat-006.json \
  --execute
```

Do not use production accounts or hosts. Do not pipe live terminal output to a
file. The writer redacts the declared SSH host, remote home path, token-shaped
strings, bearer values, passwords, API keys, and device codes; this is a final
safety layer, not permission to collect raw auth output.

Never put an Anthropic key on a harness command line or in a controller
environment passed to SSH. Provision it directly in the disposable target's
login environment before UAT-004. UAT-005 accepts only an HTTP loopback URL and
sets both upper/lowercase proxy exclusions for localhost; it cannot be pointed
at a LAN or public model server.

`write_result.py --record-id ... --criterion-id ...` can also create a redacted
manual result for aggregate release gates. Mapping alone never marks a gate as
passed; record the observed review outcome and limitations explicitly.

Every result is bound to `candidate_version`. Pass `--candidate-version` or set
`JARN_UAT_CANDIDATE_VERSION`; otherwise the writer derives the repository
project version. Tagged-release evidence should also pass `--candidate-commit`
or set `JARN_UAT_CANDIDATE_COMMIT` to the full immutable tag commit.
