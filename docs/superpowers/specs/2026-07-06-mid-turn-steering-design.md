# Mid-turn steering — design spec (T-3-8)

- **Status:** design-accepted; blueprint for implementation task **T-4-6**
- **Wave / branch:** Wave 3 · `improve/wave-3-differentiators`
- **Baseline HEAD:** `8e0a126` (wave-3: T-3-7 direct image content blocks)
- **Scope:** design only. No product code, tests, config, or user-facing docs change in T-3-8.
- **Author task brief:** `.superpowers/sdd/task-3-8-brief.md`

---

## 1. Problem & current behavior

Queued input only runs *after* the current turn finishes. There is no way to inject
guidance *into* a running turn. The streaming loop pauses only for HITL interrupts —
never for the user.

Grounded in code:

- `SessionDriver.run_turn` (`src/jarn/agent/session.py:205`) drives one turn; it delegates
  the model stream to `_stream_turn` (`session.py:307`).
- `_stream_turn` runs a `while True:` loop (`session.py:314`) that calls
  `self.agent.astream(payload, self._config(), stream_mode=["messages","updates"], subgraphs=True)`
  (`session.py:320-323`). Inside the `async for item in …astream(…)` it dispatches
  `messages` chunks (streamed tokens/tool results, `session.py:327`) and `updates` chunks
  (super-step completion, `session.py:366`). The loop only re-enters `astream` when an
  interrupt was collected (`_resume_payload`, `session.py:445`); otherwise it emits `DONE`
  and returns (`session.py:409-410`).
- The REPL, while a turn is busy, *queues* a submitted line into `InputQueue`
  (`src/jarn/repl/keys.py:92-104`) and drains it as a fresh turn only after the current
  turn ends (`src/jarn/repl/app.py:_drain_queue`, `app.py:737-759`, called from the
  `_handle` `finally` at `app.py:837`).

So the seam for mid-turn steering is the `updates` boundary inside `_stream_turn` — the
one place where a super-step (a model node or a tool/ToolNode node) has *completed* and
its result is durably checkpointed, and where re-entering `astream` is already a supported
control-flow path.

**Key invariant we rely on:** the SQLite checkpointer is not optional for the TUI.
`create_async_checkpointer` (`src/jarn/memory/sessions.py:89`) is required whenever the
graph is driven with `astream` (`sessions.py:75-78, 94-95`). Every mechanism below that
"resumes the same thread" is therefore free — the persistence it needs already exists.

---

## 2. Injection mechanism

Three candidates were evaluated. **Recommendation: (a) cooperative checkpoint.**

### (a) Cooperative checkpoint — RECOMMENDED

**Idea.** When a steer is pending, stop consuming `astream` at the next *main-graph*
`updates` boundary, append a `HumanMessage` to the thread's checkpointed state via
`agent.aupdate_state(...)`, then re-enter the same `while True:` loop with `payload=None`
so the graph resumes from the (now-augmented) checkpoint on the same `thread_id`.

**Why it fits jarn.**

- **Reuses machinery already in the repo.** `aupdate_state(self._config(), {"messages": […]})`
  is the exact call `compact_apply` (`src/jarn/controller/core.py:333`) and `fork_to_turn`
  (`core.py:457`) use to seed a thread, and `drop_pending_image_message` (`core.py:649`)
  uses to *remove* the last human message. Appending a `HumanMessage` mid-turn is the same
  operation these three already do — no new LangGraph surface.
- **Re-entering `astream` with a resume payload is already the loop's shape.** The
  `while True:` at `session.py:314` already loops back into `astream` after building a
  resume payload (`payload = _resume_payload(resolved)`, `session.py:445`). Steering adds
  one more reason to loop back (`payload = None`) alongside the existing interrupt reason.
- **Preserves in-flight tool results — the decisive property.** Because we only break at a
  completed `updates` super-step, everything the current super-step produced (assistant
  text, *and completed tool calls/results* from a ToolNode super-step) is already in the
  checkpoint. The resume reads that state back. Nothing is thrown away. This is precisely
  the property (c) cannot offer.
- **The checkpointer is already mandatory** (see §1), so there is zero added dependency.

**Cost:** requires draining/`aclose()`-ing the `astream` async generator cleanly and one
extra `aupdate_state` round-trip per steer. Both are cheap and localized to `_stream_turn`.

### (b) HITL-style dynamic interrupt — rejected

Reuse the interrupt path: `resolve_interrupts` (`src/jarn/agent/interrupts.py:33`),
`_resume_payload` (`interrupts.py:223`), `Command(resume=…)` (`interrupts.py:239-243`),
consumed at `session.py:389-445`.

**Why not.** The interrupt path is *tool-gated by construction*. An interrupt is raised by
the HITL middleware around a **specific pending tool call**; `_action_requests`
(`interrupts.py:246`) parses `action_requests` off the interrupt value and each resume is a
per-tool `{"type": "approve"|"reject"|"edit", …}` decision keyed by interrupt id
(`_resume_payload`, `interrupts.py:237-243`). There is no pending tool call at an arbitrary
steer point, so there is nothing to interrupt on and no decision schema that means "here is
some new user text." Bending this path would mean inventing a synthetic interrupt and a new
decision type, then teaching `resolve_interrupts` to route it — strictly more surface area
than (a), while *still* needing state to carry the steer text. It also can only fire where a
gated tool already pauses, which is exactly *not* where a user wants to steer (they steer
during long thinking/read phases). Rejected as more complex for less coverage.

### (c) Cancel + reseed a fresh turn with accumulated partial context — rejected

Cancel the turn task (as `/abort` does, `src/jarn/repl/app.py:_cancel_turn`, `app.py:391`),
gather the partial transcript, and start a new `run_turn` seeded with a summary + the steer.

**Why not — concrete loss.** Cancelling raises `CancelledError` through `_stream_turn`; the
`run_turn` `finally` (`session.py:300-305`) detaches the snapshot and unwinds. Any tool
super-step that had **not** reached its `updates` boundary is discarded, and — worse — a
"fresh turn seeded with accumulated context" means re-deriving state from the *display*
transcript (`TranscriptWriter`, truncated to 2 000 chars per tool output,
`sessions.py:29, 264-266`) rather than the real checkpointed messages. That drops exact
tool-call ids / structured results the model needs to continue coherently, forces the model
to re-run tools it already ran (wasted tokens and side effects), and breaks a new thread off
the old one (like `compact_apply`/`fork_to_turn`) so `/undo` / `/rewind` turn-index
bookkeeping (`session.py:468` `_current_turn_index`, `core.py:454` thread-alias) must be
re-derived. (a) keeps the real state and re-runs nothing. Rejected.

### Recommended control-flow changes for T-4-6 (mechanism a)

All changes are inside `SessionDriver` (`src/jarn/agent/session.py`); the graph, permission
engine, and interrupt path are untouched.

1. **Steer source (pull model, mirrors the existing sink pattern).** Add one field to
   `SessionDriver`:
   ```
   steer_source: Callable[[], str | None] | None = None
   ```
   It returns the next pending steer text (and clears it) or `None`. This mirrors how
   `approver`, `queue_sink`, `tool_sink`, `token_sink` are already threaded as callables —
   the driver never holds a reference to the REPL. `make_driver` (`core.py:482`) wires it
   from the controller; tests pass a lambda (see §5). A pull model is chosen over a
   `driver.steer(text)` push because the driver must consume the steer at a *safe boundary*,
   not whenever the key is pressed — the REPL writes a slot at any time, the driver reads it
   only at the boundary.

2. **Check + break at the main-graph `updates` boundary.** In `_stream_turn`, in the
   `elif mode == "updates":` arm (`session.py:366-375`), after the existing
   `for ev in self._handle_update_chunk(...)` yields, add (only when `namespace` is falsy —
   i.e. the **main** graph, never a subagent subgraph; `namespace` is already in scope from
   `_unpack_stream_item`, `session.py:324`):
   ```
   if self.steer_source is not None and not namespace:
       steer = self.steer_source()
       if steer:
           # stop the stream cleanly at this completed super-step
           await agen.aclose()
           # append the steer as a real user message on THIS thread
           from langchain_core.messages import HumanMessage
           await self.agent.aupdate_state(
               self._config(), {"messages": [HumanMessage(content=steer)]}
           )
           if self.transcript is not None:
               self.transcript.write_user(f"(steered) {steer}", ts=_time.time())
           yield Event(EventKind.NOTICE, text=f"(steered) {steer}",
                       data={"steer": True})
           payload = None          # resume from checkpoint, no new turn payload
           steered = True
           break                   # leave the async-for; re-enter the while-loop
   ```
   This requires binding the generator so it can be closed: replace
   `async for item in self.agent.astream(...)` with
   `agen = self.agent.astream(...)` + `async for item in agen:` and `steered = False`
   initialised per while-iteration.

3. **Skip DONE / interrupt handling on a steer pass.** After the `async for`, before the
   `if not interrupts:` block (`session.py:389`), add `if steered: continue`. This loops
   straight back to `astream(None, …)` — bypassing both the `DONE` emission and the
   interrupt-resolution path — so the model runs its next super-step having already seen the
   steer as the last human message in state.

4. **`_config()` is the thread anchor.** `aupdate_state` uses `self._config()`
   (`session.py:202`, `{"configurable": {"thread_id": self.thread_id}}`) — the same config
   `astream` runs under — so the append and the resume target the same checkpoint lineage.

**Cancel/append/resume sequence (one steer):**

```
astream(payload)  ──▶ … updates(super-step k, main graph) ─▶ steer_source() → "text"
                                                             │
                          await agen.aclose()   (stop stream, checkpoint k is durable)
                          await aupdate_state({messages:[HumanMessage("text")]})
                          payload = None
                          continue while-loop
astream(None)     ──▶ model node reads state (…, tool results from k, HumanMessage) ─▶ next tool call
```

**In-flight tool-result preservation restated:** the break is deferred to a *completed*
`updates` super-step, so a ToolNode's results are already checkpointed before we
`aclose()`. If a steer arrives during token streaming (`messages` mode) it is simply not
acted on until the current super-step's `updates` chunk arrives — the natural, safe seam.

---

## 3. UX

Extends the existing queue UX; no new top-level command noun.

**Today.** While busy, a submitted line is queued and echoed
`» queued: <text>` (`src/jarn/repl/keys.py:104`), managed with `/queue`
(`registry.py:161-167`; impl `src/jarn/repl/commands.py:_cmd_queue`, `commands.py:265`;
subcommands `clear` / `cancel <n>` / `move <from> <to>`).

**New.**

- **`[s] steer now` fastkey.** The `» queued:` echo (`keys.py:104`) gains a trailing hint,
  e.g. `» queued: fix the test  ·  [s] steer now`. Pressing `s` (a new key binding, added
  next to `_submit` in `keys.py`, gated by the same `live` `Condition` and by `_busy()`)
  steers the **most recently queued** line: it pops that `QueuedLine`, writes its `payload`
  into a single steer slot (see below), and echoes `› (steered) <display>`.
- **`/queue steer <n>`.** A new subcommand in `_cmd_queue` (`commands.py:265`), alongside
  `cancel`/`move`, that steers the line at 1-based index `n`. It reuses
  `InputQueue.cancel(n)` (`src/jarn/tui/input_queue.py:49`) to remove-and-return the line,
  then routes it into the steer slot. Registry `usage` string
  (`registry.py:165`) becomes `[clear|cancel <n>|move <from> <to>|steer <n>]`. (This is an
  internal wiring change to an existing command; per the brief T-3-8 ships no code, but
  T-4-6 must update the registry usage + README doc-sync in the same PR it lands.)
- **Steered render.** Steered text renders `› (steered) <text>` (distinct from a normal
  queued `» queued:` and a normal turn's `› <text>`), reusing the dim palette
  (`palette.C_DIM`). The driver also emits a `NOTICE` with `data={"steer": True}`
  (§2 step 2) so the scrollback shows where in the transcript the steer landed.

**Slot plumbing (loss-free).** The `[s]`/`/queue steer` handler sets a single
`self._steer_slot: str | None` on the REPL app. `make_driver` passes
`steer_source=self._pop_steer_slot` where `_pop_steer_slot` returns-and-clears the slot.
If a turn *ends* before the driver ever consumes the slot (the model finished — no further
`updates` boundary), `_drain_queue` (`app.py:737`) checks the leftover slot first and runs
it as the next normal turn. So a steer is never lost: it either steers the live turn or
becomes the next turn.

**Single steer slot (not a queue).** Steering is a rare, deliberate act; one pending steer
is enough. A second `[s]` before the first is consumed overwrites the slot (and re-echoes),
keeping the model from being flooded with stacked mid-turn injections.

---

## 4. Semantics

**Transcript / message ordering.** The steer lands as a `HumanMessage` appended *after* the
last completed super-step's messages (assistant text + any tool results from super-step *k*)
and *before* the next model node runs. In graph state the order is therefore:
`… → AIMessage(k) [→ ToolMessages(k)] → HumanMessage("steer") → AIMessage(k+1)`. The model
provably sees the steer before its next tool call (this is the §5 assertion). In the display
transcript, `TranscriptWriter.write_user` records it as `(steered) <text>`
(`sessions.py:217`), interleaved at the point it was injected — matching scrollback.

**Cost attribution.** A steer performs **no extra model call** itself — `aupdate_state` is a
checkpoint write. The resumed `astream(None)` continues within the *same* `run_turn`, so its
input/output tokens accrue to the same `CostTracker` and the same turn's `usage` summary
(`session.py:409`). Note the steer resume happens inside `_stream_turn`'s while-loop, **not**
at `run_turn` start, so `self._last_usage_totals.clear()` (`session.py:270`, guarded by
`if not resume:`) does **not** fire on a steer — the mid-turn cumulative-delta baseline is
correctly preserved across the resume. The steer's own text costs input tokens the next time
the model node runs, billed to the current turn. This is the honest, simple accounting: a
steered turn is one turn.

**Interaction with parallel subagents (steer the MAIN graph only).** The `not namespace`
guard (§2 step 2) is the whole story: subagent-originated `updates` arrive under a namespace
tuple whose first element is `tools:<task_id>` (T-3-5; `_agent_for_namespace`,
`src/jarn/agent/stream_handlers.py:36-62`), and `aupdate_state(self._config())` targets the
**main** thread, not a subgraph. So we only ever break/inject at a main-graph boundary and
the steer reaches the orchestrator, never a subagent. While a `task` subagent is running, the
main graph is blocked inside that super-step; a steer queued during it is applied at the next
*main* `updates` boundary (when the `task` tool returns) — i.e. the user steers the agent
that dispatched the work, which is the intended mental model.

**Cancellation / abort during a pending steer.** Two windows:

1. *Steer queued but not yet applied* (slot set, driver hasn't hit a boundary). `/abort`
   (`keys.py:86-91` → `commands.py:133-156`) cancels the turn task → `CancelledError`
   through `_stream_turn` → `run_turn` `finally` detaches the snapshot (`session.py:300-305`)
   → `settle_snapshot` + `abort_rollback` run (`commands.py:152-155`). The steer slot is
   cleared on abort (abort means "stop and roll back pending work"). Nothing was written to
   state, so nothing to undo.
2. *Steer already applied* (rare — abort races the `aclose → aupdate_state → resume` window).
   The `HumanMessage` is durably in the thread (like `drop_pending_image_message`'s
   observation that LangGraph persists immediately, `core.py:649-661`). Abort's file rollback
   does **not** touch conversation state, so the message survives into the next turn as the
   last human message. This is benign (the user *did* type it) and consistent with how a
   checkpointed message survives an aborted turn today. **Open question (§6):** whether
   T-4-6 should strip a just-applied-but-unconsumed steer on abort; recommendation is to
   leave it (simplicity, and it is visible/greppable), but flag for the owner.

   Esc/Ctrl+C cancel (`_cancel_turn(note_edits=True)`, `app.py:391`) follows the same two
   windows; it does not roll back, so an applied steer likewise persists.

**Headless — out of scope (stated).** Headless (`jarn run` / `-p`) is one-shot: a single
prompt in, result out, with no interactive input channel *during* a turn and no `InputQueue`
drain loop. There is no keyboard to press `[s]` and no concurrent line to promote — steering
is inherently interactive. `aupdate_state` from `test_headless.py` exercises state APIs, but
mid-turn steering has no trigger surface headless, so it is deliberately excluded. If a
future headless "supervisor" wants programmatic steering, the same `steer_source` field is
the injection point — but that is a separate task.

---

## 5. Test plan (for T-4-6)

Fixture-driven, in the style of `tests/test_session_stream.py` (fake agents yielding
`(namespace, mode, chunk)` triples via `_NSAgent`, `test_session_stream.py:608`) and
`tests/test_compact.py`'s `_FakeAgent` (records `aupdate_state` into `self.updated`,
`test_compact.py:18-27`). No real model, no real checkpointer.

### T1 — model sees the steer BEFORE its next tool call (the core proof)

*Setup.* A fake agent that both **streams triples** and **records state ops** in order:

- `astream(payload, config, …)` is called twice. Call 1 yields a completed main-graph
  `updates` super-step (e.g. `_task_launch`-style tool node, or a simple
  `((), "updates", {"model": {"messages": [AIMessage(...)]}})`). Call 2 (invoked with
  `payload is None`) yields the *next* super-step: a tool call, captured for the assertion.
- The agent records a log list, e.g. `self.ops`, appending `("astream", payload)` on each
  `astream` and `("update", values)` on each `aupdate_state` — so ordering is assertable.
- `steer_source` is a lambda returning `"use pytest not unittest"` on its **first** call and
  `None` thereafter (fires exactly once), mirroring how tests pass `approver`/sinks.

*Drive.* `events = [ev async for ev in driver.run_turn("write a test")]` with the driver
built like `_ns_driver` (`test_session_stream.py:620`) plus the new `steer_source`.

*Assertions.*
- `self.ops == [("astream", <turn payload>), ("update", {"messages":[HumanMessage("use pytest not unittest")]}), ("astream", None)]`
  — proving append happens *between* the two streams and the resume payload is `None`.
- The recorded `HumanMessage` content is exactly the steer text and its `type == "human"`.
- The tool call from call 2 appears in `events` (as `TOOL_START`) **after** a `NOTICE` with
  `data["steer"] is True` — the steer is visible in-transcript before the next tool.
- A single `DONE` event closes the turn (`EventKind.DONE`, one turn, not two).

### T2 — abort during a pending steer

*Setup.* A `Hang`-style agent (`test_session_stream.py:378`) whose call-1 `astream` yields
one main-graph `updates` chunk then `await asyncio.sleep(10)` on call 2 (holds the turn
open). `steer_source` returns text once. `aupdate_state` records into `self.updated`.

*Drive.* Start the turn as a task, advance it past the first `updates` boundary (so the
steer is applied and the resume `astream(None)` is hanging), then cancel:
`agen = driver.run_turn(...)`, pump `__anext__()` until the steer NOTICE is seen, then
`await agen.aclose()` (mirrors `test_snapshot_task_not_leaked_on_cancelled_turn`,
`test_session_stream.py:404`).

*Assertions.*
- `aclose()` does not raise and no `DONE` is emitted (turn was cancelled mid-resume).
- The applied-steer window behaves as specified: `self.updated` holds the `HumanMessage`
  (state append is durable) **and** the cancel unwinds cleanly (no leaked task; assert the
  `run_turn` `finally` ran — e.g. `driver._snapshot_task is None` when a checkpoint fixture
  is attached, reusing the `SlowCheckpoint`/`_DETACHED_SNAPSHOTS` pattern).
- A companion case for window 1 (steer *not* yet applied): cancel *before* the first
  `updates` boundary and assert `self.updated is None` (nothing written) — the REPL-side slot
  clear is covered by an app-level test (below).

### T3 — queue-drain interaction (leftover steer becomes the next turn)

*Setup.* REPL-app-level test (in the `test_repl_*` family that exercises `InputQueue` +
`_drain_queue`). A fake driver whose `run_turn` finishes **without** ever calling
`steer_source` (model ended before a boundary), so the steer slot is left set.

*Assertions.*
- After `[s]`/`/queue steer <n>`: the chosen `QueuedLine` is removed from `InputQueue`
  (`len` drops; `InputQueue.cancel` was used) and the steer slot holds its `payload`; the
  scrollback shows `› (steered) …`.
- When the turn ends, `_drain_queue` (`app.py:737`) consumes the leftover slot first and
  starts exactly one new turn with that payload (assert the next `_handle`/`run_turn` call
  received the steered text) — proving no-loss fallback.
- `/queue steer <n>` with an out-of-range `n` prints the usage error and leaves the queue
  and slot untouched (mirrors the existing `cancel`/`move` bounds tests,
  `commands.py:282-309`).

---

## 6. Open questions / risks (T-4-6 must resolve empirically)

1. **[TOP RISK] `aupdate_state` between two `astream` calls on the same thread + the real
   `AsyncSqliteSaver`.** The fixture tests use fake agents; they cannot prove that, against
   the real async SQLite checkpointer (`create_async_checkpointer`, `sessions.py:89`),
   `aclose()` leaves a *clean* checkpoint (no half-written super-step) and that
   `aupdate_state` then `astream(None)` on the same `thread_id` (a) does not deadlock on the
   single sqlite connection and (b) produces the exact message order in §4. T-4-6 must add a
   real-checkpointer integration check (a temp `state.sqlite`, a trivial 2-node graph) and
   verify order + no error. This is the single most important thing to validate before
   shipping.

2. **`agen.aclose()` semantics under LangGraph.** Confirm that closing the `astream` async
   generator mid-iteration cancels only the *un-started next* super-step and does not emit a
   spurious error or roll back the just-completed checkpoint. If `aclose()` proves unsafe,
   the fallback is to let the current `astream` run to completion and inject on the next
   natural loop re-entry — slower to react but strictly safe.

3. **`Command(update=…)` as an atomic alternative.** The brief notes
   `Command(update={"messages":[…]})` as the analogue of the `Command(resume=…)` used at
   `interrupts.py:239-243`. Passing `Command(update=…)` as the `astream` input would fold
   append+resume into one call (one round-trip, atomic — no abort race window from §4). We
   **recommend the two-step `aupdate_state` + `astream(None)`** for T-4-6's first cut because
   that exact append pattern is already proven in-repo (`compact_apply`/`fork_to_turn`/
   `drop_pending_image_message`), but T-4-6 should A/B it against `Command(update=…)` and
   switch if the two-step shows any checkpoint interleave issue from risk 1.

4. **No further `updates` boundary before turn end.** If the model finishes (emits its final
   answer with no tool calls) before the driver hits a main-graph `updates` boundary, the
   steer is never consumed. Covered by the slot→`_drain_queue` fallback (§3, T3), but T-4-6
   should confirm the final model node still surfaces an `updates` chunk in practice so the
   common "steer during a long turn" case fires mid-turn rather than falling through.

5. **Abort of an already-applied steer** (§4 window 2). Decide whether to strip the
   just-applied `HumanMessage` on `/abort`. Recommendation: leave it (simple, visible).
   Needs an owner call; low-stakes either way.

6. **Driver lifetime across model-rotation retries.** `_run_turn` (`src/jarn/repl/turn.py`)
   builds a **new** driver per retry attempt (`turn.py:165`). T-4-6 must pass `steer_source`
   into each attempt's `make_driver` so a steer works after a transparent fallback, and
   ensure a steer pending across a retry boundary is not double-applied (the slot is
   single-shot / cleared on consume, so this should hold — verify).

---

## 7. T-4-6 implementation checklist (files → change)

- `src/jarn/agent/session.py`
  - Add `steer_source: Callable[[], str | None] | None = None` field to `SessionDriver`.
  - `_stream_turn` (`session.py:307`): bind the `astream` generator; add the main-graph
    `updates`-boundary steer check (`not namespace`), `aclose` → `aupdate_state` →
    `payload=None` → `steered=True; break`; add `if steered: continue` before the
    `if not interrupts:` block.
  - Emit the `Event(EventKind.NOTICE, data={"steer": True})` marker + transcript write.
- `src/jarn/controller/core.py`
  - `make_driver` (`core.py:482`): wire `steer_source=` from the controller's steer slot.
- `src/jarn/repl/app.py`
  - Add `self._steer_slot: str | None`; `_pop_steer_slot`; make `_drain_queue`
    (`app.py:737`) consume a leftover slot before `pop_next()`.
- `src/jarn/repl/keys.py`
  - Add the `[s]` key binding (gated `live` + `_busy()`); append the `· [s] steer now` hint
    to the `» queued:` echo (`keys.py:104`); echo `› (steered) …`.
- `src/jarn/repl/commands.py`
  - `_cmd_queue` (`commands.py:265`): add `steer <n>` subcommand (uses `InputQueue.cancel`).
- `src/jarn/commands/registry.py`
  - `queue` spec `usage` (`registry.py:165`): add `|steer <n>`.
- `src/jarn/agent/events.py` (optional): a dedicated `EventKind.STEER` if a `NOTICE` proves
  too coarse for the renderer; otherwise reuse `NOTICE` + `data["steer"]`.
- Docs (same PR as T-4-6, per global constraints): README / README-TH `/queue` usage,
  CHANGELOG `[Unreleased]`, and the README command doc-sync test count.
- Tests: T1/T2/T3 (§5) + the real-checkpointer integration check (risk 1).

**Explicit DoD carry-over:** T-3-8 delivers this spec with a chosen mechanism (**(a)
cooperative checkpoint**) and a test plan; T-4-6 is thereby unblocked.
