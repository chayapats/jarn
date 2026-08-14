# GA release evidence workflow

This is the reproducible template for the release evidence required by
`GOAL_GENERAL_AVAILABILITY.md`. It deliberately separates a **verification
mapping** from an **observed result**: the existence of implementation or a test
command never marks a criterion as passed.

## Generate the initial matrix

```sh
python3 scripts/ga_evidence.py \
  --output artifacts/GA_RELEASE_EVIDENCE.md
```

Every acceptance, automated-test, UAT, and supplemental release-gate row starts
as `Not run`. The source-controlled mapping is
`docs/ga-evidence-map.json`; keep its implementation paths, test selectors,
platforms, and commands current as behavior changes.

## Collect UAT evidence safely

The harnesses are dry-run only unless `--execute` is explicit:

```sh
scripts/uat/uat-001-ubuntu-ssh.sh --host USER@HOST --execute
scripts/uat/uat-002-legacy-collision.sh --host USER@HOST --execute
scripts/uat/uat-003-macos-desktop.sh --host USER@MAC --execute
scripts/uat/uat-004-anthropic.sh --host USER@HOST --execute
scripts/uat/uat-005-ollama.sh --host USER@HOST --execute
scripts/uat/uat-006-network-failure.sh --host USER@HOST --execute
```

They write mode-0600 JSON under `${TMPDIR:-/tmp}/jarn-uat-results` by default.
Pass `--output` for a controlled evidence directory. Never redirect or attach
the live authentication terminal: URL/device code, account details, tokens,
prompt text, and raw authentication output are intentionally excluded.

## Build and enforce the report

```sh
python3 scripts/ga_evidence.py \
  --evidence-dir artifacts/ga-evidence \
  --candidate-version 1.0.6 \
  --output artifacts/GA_RELEASE_EVIDENCE.md

python3 scripts/ga_evidence.py \
  --evidence-dir artifacts/ga-evidence \
  --candidate-version 1.0.6 \
  --candidate-commit FULL_TAGGED_COMMIT_SHA \
  --strict \
  --output artifacts/GA_RELEASE_EVIDENCE.md
```

`--strict` exits non-zero if even one row is failed, blocked, or not run. It also
rejects malformed evidence, unknown criterion IDs, or evidence that does not
declare that secrets and raw authentication output are prohibited. The candidate
version defaults to `JARN_GA_CANDIDATE_VERSION` or the project version. A commit
is enforced only when `--candidate-commit` or `JARN_GA_CANDIDATE_COMMIT` is set.
Passed evidence with a different version, a missing required commit, or a
different commit is ignored and cannot satisfy strict mode.

Each result artifact supplies the required observed fields:

| Field | Meaning |
|---|---|
| `candidate_version` | Exact version exercised by this observation; required |
| `candidate_commit` | Optional full 40/64-character Git commit exercised by this observation |
| `criterion_ids` | Stable goal IDs supported by this observation |
| `status` | `passed`, `failed`, `blocked`, or `not_run` |
| `platform` | OS, version, architecture, and libc |
| `command` | Reproducible, redacted command |
| `result` | Concise observed outcome, not an expectation |
| `limitations` | Remaining constraints or missing coverage |
| `redaction` | Machine-checkable declaration that secrets, raw auth, and raw terminal output are absent |

Use `scripts/uat/result.template.json` and
`scripts/uat/result.schema.json` when creating a manually reviewed result. The
atomic writer also supports non-UAT gates with `--record-id` plus one or more
`--criterion-id` arguments. Keep raw result files as protected release
artifacts; publish only after a secret scan and human review.

`write_result.py` accepts `--candidate-version` and `--candidate-commit`. When
the version is omitted it derives `JARN_UAT_CANDIDATE_VERSION` or the repository
project version; the optional commit may also come from
`JARN_UAT_CANDIDATE_COMMIT`. For tagged-release evidence, always supply both
exact values so results cannot be reused across candidates.
