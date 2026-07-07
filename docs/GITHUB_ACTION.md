# J.A.R.N. GitHub Action

Run J.A.R.N. in CI headless mode (`-p`) from any GitHub Actions workflow.

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `prompt` | **yes** | — | The prompt to send to the agent. |
| `api_key` | **yes** | — | LLM provider API key. **Always source from `secrets.*`** — never hardcode. |
| `model` | no | _(project config)_ | OpenRouter model slug (e.g. `anthropic/claude-opus-4-8`). Overrides the project config model for this run. |
| `permission_mode` | no | `auto-edit` | Permission mode: `auto-edit`, `yolo`, `ask`, or `plan`. |
| `max_turns` | no | `15` | Maximum agent turns. |
| `preset` | no | `ci` | Named policy preset. See [Preset note](#preset--docker-requirement) below. |

## Outputs

| Output | Description |
|--------|-------------|
| `result` | The agent's final text reply. |
| `cost_usd` | Total session cost in USD (from the `--json` envelope `cost` field). |
| `turns` | Number of agent turns completed. |

The action calls `jarn` with `--json`, which emits a JSON envelope:

```json
{"result": "…", "tokens": {…}, "cost": 0.0042, "turns": 3, "tool_calls": 7}
```

`result` and `turns` are forwarded as-is; `cost` is exposed as `cost_usd`.

## Preset & Docker requirement

The default `ci` preset uses `permission_mode: yolo` and the Docker backend
(`backend: docker`).  **Docker is required.**  Ubuntu Actions runners
(`ubuntu-latest`) ship with Docker pre-installed.

If Docker is absent, jarn fails closed with a clear `"Docker is not available"`
error — it never silently falls back to bare-host execution with yolo mode.

### Docker-less runners (macOS / Windows / self-hosted)

Override the preset and permission mode to avoid the Docker requirement:

```yaml
- uses: ./action
  with:
    preset: "trusted-repo"
    permission_mode: "auto-edit"
    prompt: "…"
    api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

`trusted-repo` uses `permission_mode: ask` with the sandbox disabled.  Pair it
with `permission_mode: auto-edit` (passed here) to allow unattended file writes
without Docker.

## Secrets setup

1. Go to **Settings → Secrets and variables → Actions → New repository secret**.
2. Add `OPENROUTER_API_KEY` (or your provider's key name).
3. Reference it in the workflow: `api_key: ${{ secrets.OPENROUTER_API_KEY }}`.

Never paste an API key literally in a workflow file.  GitHub scans for secrets
and will alert you, and anyone with read access to the repo can read workflow
files.

## Example: PR review bot

See [`examples/github/pr-review.yml`](../examples/github/pr-review.yml) for a
complete example that:

- Triggers on every pull request open / push.
- Computes `git diff origin/$BASE...HEAD` and feeds it to the agent.
- Posts (or updates) a **single sticky comment** on the PR using
  `gh api` with an idempotent find-existing-by-marker pattern.
- Uses `preset: review-only` (plan mode — read-only, no file writes).
- Permissions: `contents: read`, `pull-requests: write` (least-privilege).

```yaml
permissions:
  contents: read
  pull-requests: write

- uses: ./action
  with:
    prompt: "Review this diff: …"
    preset: "review-only"
    permission_mode: "plan"
    max_turns: "5"
    api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

## Example: Issue-fix bot

See [`examples/github/issue-fix.yml`](../examples/github/issue-fix.yml) for a
complete example that:

- Triggers on `issue_comment` events containing `@jarn`.
- **Actor allowlist guard:** only runs for comments from `OWNER`, `MEMBER`, or
  `COLLABORATOR` (via `author_association`).  Arbitrary public users cannot
  trigger it.
- Checks out the repo, runs jarn with the comment body as the prompt, commits
  the resulting changes to a new branch, and opens a PR.
- Permissions: `contents: write`, `pull-requests: write` (least-privilege).

```yaml
permissions:
  contents: write
  pull-requests: write

if: >
  contains(github.event.comment.body, '@jarn') &&
  contains(
    fromJSON('["OWNER", "MEMBER", "COLLABORATOR"]'),
    github.event.comment.author_association
  )
```

### Trust model

The issue-fix bot grants write access to any comment that matches the allowlist
check, on any issue — including issues opened by non-members.  Before enabling:

- Confirm your `author_association` allowlist is restrictive enough for your
  repo's contributor model.
- Consider using a fine-grained Personal Access Token (`secrets.BOT_PAT`) with
  only the required scopes instead of the default `GITHUB_TOKEN` if your branch
  protection rules block the default token from pushing.
- Review every generated PR before merging — the agent operates in yolo mode.

## Versioning

The action pins `jarn-cli@<major>.<minor>` (currently `jarn-cli@0.5`).  The
test `tests/test_ci.py::test_action_yaml_valid` asserts this pin matches
`src/jarn/version.py` at all times — a drift guard that fails CI before a
mis-matched package ships.

## actionlint

The `.github/workflows/ci.yml` `actionlint` job lints `action/action.yml` and
`examples/github/` on every push/PR using a pinned `actionlint` binary.  This
job runs only in GitHub CI (not locally) because it downloads the binary via
`curl`.  To run locally, install actionlint separately:

```bash
brew install actionlint   # macOS
# or: go install github.com/rhysd/actionlint/cmd/actionlint@latest
actionlint action/action.yml examples/github/pr-review.yml examples/github/issue-fix.yml
```
