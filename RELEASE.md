# Release process — v0.8.0 alpha

Checklist for publishing J.A.R.N. to PyPI, npm, and GitHub Releases.

Still **alpha** (`Development Status :: 3 - Alpha`) — v1.0.0 is not yet earned;
see CHANGELOG §0.3.0 for the remaining road-to-1.0 work.

## Automated gates (must pass)

```bash
uv sync --extra dev
uv run ruff check src tests scripts
uv run mypy src/
uv run pytest -q                    # 1673 tests
uv run pytest tests/test_packaging.py -q
uv build
```

`tests/test_packaging.py` verifies:

- sdist excludes `.jarn` / sqlite / venv artifacts
- wheel contains `repl.py`, `cli.py`, and entry points
- clean venv install → `jarn --version` + `jarn doctor --json`

## Manual QA (pre-tag)

Run on a **fresh machine or clean venv** with a real API key. Record date + result.

| Step | Command / action | Pass? |
|------|------------------|-------|
| 1 | `uv tool install jarn` or `pip install jarn==0.8.0` (or `npm install -g jarn-cli`) | ☐ |
| 2 | `jarn --version` → `jarn 0.8.0` | ☐ |
| 3 | `jarn setup` — wizard completes, `~/.jarn/config.yaml` created | ☐ |
| 4 | `jarn doctor` — providers OK, extensions section renders | ☐ |
| 5 | `cd <project>` → `jarn` — REPL launches, splash visible | ☐ |
| 6 | `/help` — no Rich markup crash; usage hints visible | ☐ |
| 7 | `/` + Tab — command menu with descriptions | ☐ |
| 8 | One chat turn with real model — streams response | ☐ |
| 9 | Untrusted repo with hooks in `.jarn/config.yaml` — trust prompt; decline → safe | ☐ |
| 10 | `jarn trust <path>` → project hooks honoured after approval | ☐ |
| 11 | Untrusted repo → launch shows the untrusted notice; `/trust` lifts the floor | ☐ |
| 12 | `/mcp status` — lists configured MCP servers (or "no MCP servers configured") | ☐ |
| 13 | **T-4-8 demo GIF** — `./scripts/record-demo.sh` → `docs/assets/demo.gif` < 3 MB; README preview renders | ☐ |

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
3. Tag and push (the tag drives the whole release):

```bash
git tag v0.8.0
git push origin v0.8.0
```

4. GitHub Actions `Release` workflow then, from that tag:
   - publishes the PyPI sdist + wheel (`skip-existing`, so re-runs are no-ops);
   - builds the three standalone binaries (linux-x64, linux-arm64, macos-arm64)
     and attaches them to the GitHub Release;
   - assembles and publishes the npm packages — `jarn-cli` + the three
     `jarn-cli-<platform>` binary packages — via the `npm` job.

> npm publish runs **without `--provenance`** while the repo is private; re-enable
> it (and `id-token: write`) once the repo is public. Intel macOS (`macos-13`) is
> intentionally not built — its GitHub runner is deprecated.

## Post-release

- Verify `pip install jarn` / `uv tool install jarn` from PyPI
- Verify `npm install -g jarn-cli` → `jarn --version`
- Open GitHub Release notes (copy from the latest CHANGELOG section)

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
| Node tests (launcher + assembly) | ✅ 8 + 7 passed (CI `npm` job) |
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
