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
uv run python scripts/eval.py --update-baseline  # record current pass/fail
```

This **drives a real model and costs tokens.** It requires a configured key
(`jarn setup` first); if no config is found the runner prints a note and exits
`2` (skip, not failure). It is **not** run in CI — the offline harness logic is
covered by `tests/test_eval_harness.py` instead.

## Baseline

Default mode compares against `baseline.json` and exits non-zero if any fixture
that **passed** in the baseline now fails (a regression). With no baseline file
it just prints a note and exits `0`. No `baseline.json` is committed (it holds
machine-specific live numbers and is gitignored) — create one locally with
`--update-baseline` after a successful run.
