## Summary

<!-- One-paragraph description of what this PR does and why. -->

## Changes

<!-- Bullet list of key changes. -->

## Test plan

- [ ] `uv run ruff check src tests scripts` clean
- [ ] `uv run mypy src/` clean
- [ ] `uv run pytest -q` — all tests pass, no new failures

## Checklist

- [ ] Docs updated (README, CHANGELOG `[Unreleased]`) where relevant
- [ ] New user-visible config keys added to `config/schema.py` **and** `config/pydantic_schema.py`
- [ ] New slash commands registered in `commands/registry.py`
- [ ] Any printed output passes through `redact_secrets`
