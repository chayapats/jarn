# Release process — General Availability candidate

Fail-closed checklist for promoting one identical J.A.R.N. candidate to GitHub,
PyPI, and npm. This source line targets v1.0.10; the registries and GitHub Releases
remain the authority for current publication status. A tag push cannot promote a
draft by itself: every criterion, UAT, and published-artifact gate must pass first.

## Automated gates (must pass)

```bash
uv sync --extra dev --extra telegram
uv run ruff check src tests scripts
uv run mypy src/
uv run pytest -q                    # 3368 tests
uv run pytest tests/test_packaging.py -q
uv run pytest tests/test_installer.py tests/test_ci.py tests/test_update.py -q
uv run python scripts/benchmark_startup.py --output artifacts/startup.json
uv run python scripts/ga_evidence.py \
  --evidence-dir artifacts/ga-evidence \
  --candidate-version 1.0.10 \
  --strict
uv build
```

`tests/test_packaging.py` verifies:

- sdist excludes `.jarn` / sqlite / venv artifacts
- wheel contains `repl.py`, `cli.py`, and entry points
- clean venv install → `jarn --version` + `jarn doctor --json`

## Manual QA

### UAT evidence (pre-promotion)

Run each harness with its explicit `--execute` guard on a disposable target and save
only the validated/redacted JSON result. Dry-run output is never Pass evidence.

| Scenario | Harness | Required evidence |
|---|---|---|
| Ubuntu 22.04/glibc 2.35 over SSH | `scripts/uat/uat-001-ubuntu-ssh.sh` | clean one-line install, visible auth, live catalog, first prompt |
| Legacy npm/Python collision | `scripts/uat/uat-002-legacy-collision.sh` | correct resolution, retained prior, config migration backup |
| macOS desktop ChatGPT | `scripts/uat/uat-003-macos-desktop.sh` | browser/fallback challenge and verified account/model |
| Anthropic API-key path | `scripts/uat/uat-004-anthropic.sh` | reference-only secret, disclosure, verified turn |
| Ollama local/offline | `scripts/uat/uat-005-ollama.sh` | local live catalog/turn and missing-model remediation |
| Network failure/retry | `scripts/uat/uat-006-network-failure.sh` | no false Done, retry action, prior hashes unchanged |

The evidence report must map every MUST/TEST/UAT criterion and contain zero P0
failures, zero secret/path leaks, and no invented Pass rows.

Optional binary smoke (maintainer):

```bash
./scripts/build-binary.sh
./dist/jarn --version
./dist/jarn doctor --json
```

## Publish

1. Credentials are already configured in the repo: **PyPI Trusted Publishing**
   (OIDC, no token) via the `pypi` environment, and **`NPM_TOKEN`** stored as a
   secret in the `NPM_TOKEN` environment (an npm **automation** token). No tokens
   live in the workflow.
2. Bump the version in `pyproject.toml` + `src/jarn/version.py`, run `uv lock`,
   update `CHANGELOG.md`, and merge to `main`.
3. Tag and push (the tag creates a **draft**, not a public release):

```bash
git tag -a v1.0.10 -m "J.A.R.N. v1.0.10"
git push origin v1.0.10
```

4. GitHub Actions builds binaries/packages plus the exact tagged `install.sh`, creates
   sorted checksums, SPDX and CycloneDX SBOMs, and provenance/attestations where the
   repository supports them. Ubuntu 20.04 executes every Linux artifact.
5. The workflow attaches assets to a draft and runs authenticated clean-install,
   glibc/npm-shadow, historical two-version update, real `jarn update`, rollback,
   itemized uninstall, reinstall, data-preservation, and integrity canaries.
6. The tag workflow intentionally stops at a verified draft unless the repository
   variable `JARN_GA_PROMOTE_TAG` equals the exact immutable tag. Run all protected
   UAT harnesses against that tagged candidate, commit the redacted evidence to
   `main` without moving or replacing the tag, and require the final evidence gate
   to pass before setting that variable:

```bash
tag_commit=$(git rev-list -n 1 v1.0.10)
python scripts/ga_evidence.py \
  --evidence-dir artifacts/ga-evidence \
  --candidate-version 1.0.10 \
  --candidate-commit "$tag_commit" \
  --strict
```
7. Rerun the same tag workflow only after the strict evidence gate. Only
   `promote_release` may make the draft public; PyPI/npm consume the already verified
   internal artifacts after the anonymous public-URL canary passes. Clear
   `JARN_GA_PROMOTE_TAG` immediately afterward. A failed or skipped gate leaves a
   draft and publishes no package-registry artifact.

## Post-release

- Fetch the public tagged installer/checksum/assets and repeat the published URL canary.
- Verify fresh PyPI and npm installs report the identical promoted version/digests.
- Attach the validated GA evidence report; record any rollback/yank decision.

## v0.10.0 sign-off (2026-08-08) — RELEASED ✅

Telegram gateway v1 plus release-pipeline repair. The action self-reference was
updated to `jarn-cli@0.10`, and the refreshed `NPM_TOKEN` published all npm packages.

| Gate | Result |
|------|--------|
| pytest (full) | ✅ 2402 passed, 1 skipped (2403 collected) |
| ruff + mypy | ✅ clean; mypy checked 147 source files |
| Node tests | ✅ 17 passed |
| `tests/test_packaging.py` | ✅ 4 passed |
| `uv build` | ✅ sdist + wheel produced |
| CI on main | ✅ [run 31248864086](https://github.com/chayapats/jarn/actions/runs/31248864086), attempt 2, all 10 jobs green; attempt 1 had one transient Windows append-stress miss |
| Release workflow | ✅ [run 31249506719](https://github.com/chayapats/jarn/actions/runs/31249506719), first attempt |
| PyPI | ✅ `jarn==0.10.0`; fresh temporary install reports `jarn 0.10.0` |
| npm | ✅ `jarn-cli@0.10.0` + linux-x64/linux-arm64/darwin-arm64 packages; fresh temporary install reports `jarn 0.10.0` |
| GitHub Release | ✅ [`v0.10.0`](https://github.com/chayapats/jarn/releases/tag/v0.10.0) with linux-x86_64, linux-arm64, and macos-arm64 binaries |

## v0.1.0 sign-off (2026-06-08)

| Gate | Result |
|------|--------|
| pytest (full) | 371 passed |
| ruff + mypy | clean |
| `tests/test_packaging.py` | passed (automated wheel/sdist smoke) |
| `uv build` | sdist + wheel produced (`dist/jarn-0.1.0-py3-none-any.whl`) |
| `./scripts/build-binary.sh` | `dist/jarn` → `jarn 0.1.0` (macOS arm64, 2026-06-08) |
| Manual QA rows 1–10 | run by maintainer before `git push origin v0.1.0` |

## v0.2.0 sign-off (2026-06-09)

| Gate | Result |
|------|--------|
| pytest (full) | 602 passed |
| ruff + mypy | clean |
| `tests/test_packaging.py` | passed (automated wheel/sdist smoke) |
| `uv build` | sdist + wheel produced (`dist/jarn-0.2.0-py3-none-any.whl`) |
| Manual QA rows 1–10 | run by maintainer before `git push origin v0.2.0` |

## v0.3.0 sign-off (pending — superseded by v0.4.0)

| Gate | Result |
|------|--------|
| pytest (full) | 778 passed |
| ruff + mypy | clean |
| `tests/test_packaging.py` | ✅ 3 passed (2026-06-09) |
| `uv build` | ✅ `dist/jarn-0.3.0-py3-none-any.whl` + `.tar.gz` (2026-06-09) |
| Manual QA rows 1–12 | ☐ run by maintainer before `git push origin v0.3.0` |
| git commit + tag `v0.3.0` + PyPI publish | ☐ maintainer (not yet committed) |

## v0.4.0 sign-off (2026-06-18) — RELEASED ✅

| Gate | Result |
|------|--------|
| pytest (full) | 1166 passed, 8 skipped |
| ruff + mypy | clean |
| `tests/test_packaging.py` | ✅ passed |
| `uv build` | ✅ `dist/jarn-0.4.0-py3-none-any.whl` + `.tar.gz` |
| CI on main | ✅ green (after the traceback-pointer soft-wrap fix, PR #3) |
| tag `v0.4.0` + PyPI publish | ✅ published — PyPI latest `jarn 0.4.0`; GitHub release `v0.4.0` with linux/macos binaries |

## v0.4.4 sign-off (2026-06-18) — RELEASED ✅ (first npm release)

Added npm distribution (`jarn-cli`). The npm publish took three tries to land —
0.4.1 stalled on the deprecated Intel runner, 0.4.2 failed `ENEEDAUTH` (the npm
job had no `environment:`), 0.4.3 failed `E422` (`--provenance` needs a public
repo). 0.4.4 fixes all three; 0.4.1–0.4.3 are PyPI-only interims.

| Gate | Result |
|------|--------|
| pytest (full) | ✅ 1166 passed, 8 skipped |
| Node tests (launcher + assembly) | ✅ 10 + 7 passed (CI `npm` job) |
| ruff + mypy | ✅ clean |
| `uv build` | ✅ `dist/jarn-0.4.4-*.whl` + `.tar.gz` |
| PyPI publish | ✅ `jarn 0.4.4` |
| GitHub Release `v0.4.4` | ✅ binaries: linux-x64, linux-arm64, macos-arm64 |
| npm publish | ✅ `jarn-cli@0.4.4` + `jarn-cli-{linux-x64,linux-arm64,darwin-arm64}@0.4.4` |
| End-to-end | ✅ `npm i jarn-cli` on macOS arm64 → `jarn --version` → `jarn 0.4.4` |

## v0.5.0 sign-off (2026-07-02) — RELEASED ✅

Headless multi-turn, OTel tracing, cross-platform image paste, arg-aware slash
completion, verify gate, context token budgets, Pydantic config validation
(`config_version` + migrators), and CI hardening — see CHANGELOG §0.5.0.

| Gate | Result |
|------|--------|
| pytest (full) | ✅ 1347 passed |
| ruff + mypy | ✅ clean |
| `uv build` | ✅ `dist/jarn-0.5.0-*.whl` + `.tar.gz` |
| PyPI publish | ✅ `jarn 0.5.0` |
| GitHub Release `v0.5.0` | ✅ binaries: linux-x64, linux-arm64, macos-arm64 |
| npm publish | ✅ `jarn-cli@0.5.0` + `jarn-cli-{linux-x64,linux-arm64,darwin-arm64}@0.5.0` |
