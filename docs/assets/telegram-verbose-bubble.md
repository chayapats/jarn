# Telegram quiet default vs `/verbose` bubble (P5-4)

Shipped in [#94](https://github.com/chayapats/jarn/pull/94) (P2). Default
quiet turn is the #40 contract and stays the default.
`gateway.telegram.tool_progress` defaults to `off` and does **not** inherit
CLI `ui.tool_progress: new`.

Prose still uses `sendMessageDraft` → `sendMessage`. The progress bubble is
a **third channel**: one `sendMessage` + `editMessageText`. It must not
share a message id with the draft. After finalize, the bubble is deleted
when `gateway.telegram.tool_progress_cleanup` is `delete` (default).

HTML dialect via `tui/layout.py` (`dialect="html"`). Glyphs: ⏺ tool, ⎿ result.

## Quiet default (before `/verbose`, and the default forever)

Operator sends a prompt. Telegram shows only the streaming prose draft, then
the finalized answer. No tool lines.

```text
You: list the files in src/jarn/tui and summarize toolbar.py

[draft …]
The toolbar lives in toolbar.py. It shows model, mode, context, and cost.
[finalized message — same prose]
```

`/verbose` with no following turn only updates session density. The next
**turn** is what can show a bubble.

## After `/verbose` (one edited progress bubble)

```text
You: /verbose
(bot): Tool progress  new
       Session only — persist with /config set gateway.telegram.tool_progress.

You: list the files in src/jarn/tui and summarize toolbar.py

[progress bubble — one message, edited in place]
⏺ glob  glob_pattern=src/jarn/tui/*.py
  ⎿ 8 files · 0.2s
⏺ read_file  path=src/jarn/tui/toolbar.py
  ⎿ 226 lines · 0.4s

[finalized answer — separate message]
The toolbar lives in toolbar.py. It shows model, mode, context, and cost.

[progress bubble deleted]
```

Density `new` is one ⏺/⎿ pair per distinct tool (tails stay off). Density
`verbose` may include tails. Subagent inner stream (`data.agent`) is still
dropped.

Long quiet turn (no tool events, `long_running_notifications: true`, ~3 min):

```text
[progress bubble]
Working — 3 min
```
