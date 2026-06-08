---
name: review
description: Review the current git diff for correctness bugs.
---
Review the current git diff (staged and unstaged). Trace the actual code path, not
just the lines that changed. Focus on:
- correctness bugs and edge cases
- error handling and resource cleanup
- anything that contradicts the project's conventions in JARN.md

Be concise and specific (file:line). Extra focus on: $ARGS
