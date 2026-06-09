# Release process — v0.3.0 alpha

Checklist for publishing J.A.R.N. to PyPI and GitHub Releases.

Still **alpha** (`Development Status :: 3 - Alpha`) — v1.0.0 is not yet earned;
see CHANGELOG §0.3.0 for the remaining road-to-1.0 work.

## Automated gates (must pass)

```bash
uv sync --extra dev
uv run ruff check src tests scripts
uv run mypy src/
uv run pytest -q                    # 777 tests
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
| 1 | `uv tool install jarn` or `pip install jarn==0.3.0` | ☐ |
| 2 | `jarn --version` → `jarn 0.3.0` | ☐ |
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

Optional binary smoke (maintainer):

```bash
./scripts/build-binary.sh
./dist/jarn --version
./dist/jarn doctor --json
```

## Publish

1. Ensure `PYPI_TOKEN` is set in GitHub repository secrets.
2. Commit release prep on `main`.
3. Tag and push:

```bash
git tag -a v0.3.0 -m "v0.3.0 — alpha (Docker backend, policy profiles, smoke-eval, /mcp + /trust)"
git push origin main
git push origin v0.3.0
```

4. GitHub Actions `Release` workflow builds PyPI artifacts + per-OS binaries and
   attaches them to the GitHub Release.

## Post-release

- Verify `pip install jarn` / `uv tool install jarn` from PyPI
- Open GitHub Release notes (copy from `CHANGELOG.md` §0.3.0)

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

## v0.3.0 sign-off (pending)

| Gate | Result |
|------|--------|
| pytest (full) | 777 passed |
| ruff + mypy | clean |
| `tests/test_packaging.py` | ✅ 3 passed (2026-06-09) |
| `uv build` | ✅ `dist/jarn-0.3.0-py3-none-any.whl` + `.tar.gz` (2026-06-09) |
| Manual QA rows 1–12 | ☐ run by maintainer before `git push origin v0.3.0` |
| git commit + tag `v0.3.0` + PyPI publish | ☐ maintainer (not yet committed) |
