# Smoke evals

Tiny end-to-end checks: each fixture under `fixtures/<name>/` is a broken seed
project. The runner copies the seed to a temp dir, drives **one** headless JARN
agent turn against it, then scores the result by running the fixture's checker.

```
fixtures/<name>/
  repo/        seed (broken) state the agent edits
  _solution/   reference fix (determinism + offline tests only)
  eval.yaml    { name, prompt, checker, timeout_s, protected }
```

**Anti-gaming.** `protected` lists the authoritative files (tests / graders).
After the agent runs, the runner restores them from the seed *before* scoring,
so an agent can't "pass" by rewriting the tests to match buggy output. The
`add-missing-test` grader goes further and grades by *mutation* — the agent's
test must pass against the correct module **and** fail against a broken one.

## Run it

```bash
uv run python scripts/eval.py                  # run all, compare to baseline
uv run python scripts/eval.py --fixture NAME   # run one
uv run python scripts/eval.py --json           # machine-readable output
uv run python scripts/eval.py --update-baseline  # record current pass/fail (per-fixture)

# Write the summary JSON and compare against the committed baseline:
uv run python scripts/eval.py --summary evals/latest.json
uv run python scripts/eval.py --compare evals/baseline.json  # exits non-zero if >1 regression
```

This **drives a real model and costs tokens.** It requires a configured key
(`jarn setup` first); if no config is found the runner prints a note and exits
`2` (skip, not failure). It is **not** run in PR CI — the offline harness logic is
covered by `tests/test_eval_harness.py` instead.

## Summary JSON format

`eval.py --summary <path>` writes (and `--compare <baseline>` reads) a summary
in this shape:

```json
{
  "pass":  4,
  "fail":  0,
  "total": 4,
  "model": "openrouter/deepseek/deepseek-chat",
  "cost":  0.012
}
```

`scripts/eval-badge.py <summary.json>` converts this to a shields.io ENDPOINT
JSON (`{"schemaVersion":1, "label":"evals", "message":"4/4 nightly", "color":"green"}`).

## Regression gate

`--compare <baseline.json>` exits **non-zero** when `baseline.pass − current.pass > 1`.
A tolerance of ≤ 1 is intentional: live-LLM evals are inherently flaky (rate
limits, non-determinism). The nightly CI job reads this exit code to decide
whether to **open a pinned issue** — it does NOT fail CI red (the `continue-on-error`
job flag stays in place).

## Baseline (`evals/baseline.json`)

`evals/baseline.json` is committed in the **summary format** above. The current
file is a **placeholder** generated from the four existing fixtures (all passing)
with `cost: 0.0` because no live run has been captured yet.

**Before relying on the regression gate**, run a real nightly eval and commit the
result:

```bash
uv run python scripts/eval.py --summary evals/baseline.json
git add evals/baseline.json && git commit -m "chore: refresh eval baseline"
```

## Nightly workflow

`.github/workflows/nightly.yml` runs a small fixture set on a daily schedule when:
- The `NIGHTLY_EVAL_ENABLED` repo secret is set to `"true"`.
- The `EVAL_API_KEY` secret is available in the `nightly-eval` GitHub environment
  (same environment-gate pattern as the npm publish job in `release.yml`).

### What the job does

1. Runs `eval.py --summary evals/nightly/<date>.json` against the pinned model
   (`openrouter/deepseek/deepseek-chat`).
2. Copies to `evals/latest.json`.
3. Runs `eval-badge.py` to produce `evals/badge.json` (shields.io endpoint JSON).
4. Compares the current result to `evals/baseline.json` using `compare_summaries`.
5. If regression > 1 task: **opens or updates a pinned issue** labelled
   `nightly-regression` (label must exist in the repo). CI stays green.
6. Commits `evals/nightly/<date>.json`, `evals/latest.json`, and `evals/badge.json`
   to the dedicated **`eval-results`** branch (keeps `main` clean). The branch is
   created as an orphan on first run.

### README badge

The README badge reads from the `eval-results` branch:

```markdown
![evals](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/chayapats/jarn/eval-results/evals/badge.json)
```

It renders as **"evals: 4/4 nightly"** once the first nightly run completes and
pushes `evals/badge.json`.

### Forced-fail dry run

Trigger a `workflow_dispatch` with `force_regression: true` to simulate a >1-task
regression without spending tokens. The job overwrites the nightly summary with
`{"pass":0,...}` before the comparison step, exercising the pinned-issue path.
