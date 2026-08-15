# Telegram second-DM `Working…` ack (P5-4)

Shipped in [#98](https://github.com/chayapats/jarn/pull/98) (P4-2 / P4-3).
A second DM while a turn is in flight **steers** by default
(`gateway.telegram.busy_input_mode: steer`). It does **not** inherit CLI
`ui.busy_input_mode: queue`. It never starts a second `_run_turn`
(T-QA-1: one in-flight turn task).

Ack copy is the fixed string `Working…` (`BUSY_ACK_TEXT`). Detail
paragraphs (`Steering into the current turn.` / `Queued until this turn
finishes.`) are off unless `ui.busy_ack_detail` or
`gateway.telegram.busy_ack_detail` is true.

The ack reuses the progress-bubble channel (one edit), not a new essay
message and not the prose draft.

## After (default steer, detail off)

Turn already running from the first DM. Operator sends a second DM:

```text
You: refactor toolbar.py to drop the title first
     (turn in flight — draft is live)

You: skip tests, just the toolbar
[progress bubble — one short edit]
Working…

(agent continues the same turn with the steer)
[finalized answer]
[progress bubble deleted]
```

## After, with detail on (opt-in)

```text
You: skip tests, just the toolbar
[progress bubble]
Working…
Steering into the current turn.
```

## Queue overlay (`gateway.telegram.busy_input_mode: queue`)

```text
You: skip tests, just the toolbar
[progress bubble]
Working…
```

With detail on, the extra line is `Queued until this turn finishes.` The
queued line drains after `done`. Still one in-flight turn.

There is no useful “before” screenshot in the A–K grammar: the previous
behavior was `ErrorFrame code="busy"` (the second DM was rejected), not a
Hermes-style steer/queue ack.
