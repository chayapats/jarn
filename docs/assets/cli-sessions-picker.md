# CLI `/sessions` picker (P5-4)

Shipped in [#96](https://github.com/chayapats/jarn/pull/96) (P1-4). Telegram
keeps the HTML/text list from `cmd_sessions` (no prompt_toolkit overlay on
the gateway). `/resume <id>` on Telegram is P3 ([#95](https://github.com/chayapats/jarn/pull/95)).

Labels are `{updated}  {title}  {thread_id[:8]}` from `session_label()`.

## Before (waves A–K)

`/sessions` printed a text list and told you to pick with `/resume`. Only
`/resume` (no args) opened the arrow-key picker.

```text
Sessions
  2h ago  Fix toolbar  a1b2c3d4
  yesterday  (untitled)  e5f6g7h8

use /resume to pick one
```

```text
› /resume
  2h ago  Fix toolbar  a1b2c3d4
  yesterday  (untitled)  e5f6g7h8
  Cancel
```

## After (P1)

REPL `/sessions` **is** the picker. `/resume` is the same command. Empty
query = full list. `/sessions [q]` filters `session_label` / title /
thread-id prefix.

```text
› /sessions
  2h ago  Fix toolbar  a1b2c3d4
  yesterday  (untitled)  e5f6g7h8
  Cancel
```

```text
› /sessions toolbar
  2h ago  Fix toolbar  a1b2c3d4
  Cancel
```

```text
› /sessions zzzz
No sessions matching 'zzzz'.
```

After a pick (or `--resume` / continue-into-a-thread), a **local** recap
prints — the same facts `/status` already builds. No LLM (P1-2).

```text
── resumed: 12 messages ──
  Directory   /srv/repos/jarn
  Model       openrouter/anthropic/claude-opus-4-8
  Mode        ask
  Last turn   Fix toolbar
```

Non-TTY / Telegram still get the text list from `cmd_sessions` (with the
same `[q]` filter). That is intentional.
