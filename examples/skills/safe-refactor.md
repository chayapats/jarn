---
name: safe-refactor
description: Refactor code in small, verified steps without changing behavior.
trigger: auto
---
When asked to refactor:

1. First read the target code and its tests. If there are no tests, say so and
   propose adding characterization tests before changing anything.
2. Make one small, behavior-preserving change at a time.
3. After each change, run the project's test suite (use the detected verify
   command) and report the result.
4. Never mix a refactor with a behavior change in the same step. If a behavior
   change is needed, call it out separately.
5. Summarize what changed and why, and confirm tests are green before reporting done.
