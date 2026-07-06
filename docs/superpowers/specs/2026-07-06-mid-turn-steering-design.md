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
control-flow path. **Crucial refinement (developed in §2):** not every completed
`updates` super-step is a *safe* injection point. A model-node super-step completes with an
`AIMessage` whose `tool_calls` are still **pending** (unexecuted); the seam we actually
inject at is a *settled tool boundary* — one where the last checkpointed message is not an
`AIMessage` awaiting its `ToolMessage`s.

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

2. **Check + break ONLY at a *settled* main-graph tool boundary.** In `_stream_turn`, in the
   `elif mode == "updates":` arm (`session.py:366-375`), after the existing
   `for ev in self._handle_update_chunk(...)` yields, add the gated injection below. Two
   guards matter equally:
   - `not namespace` — the **main** graph only, never a subagent subgraph (`namespace` is
     already in scope from `_unpack_stream_item`, `session.py:324`).
   - **settled** — the last checkpointed message must NOT be an `AIMessage` with
     unfulfilled `tool_calls`. **This is the load-bearing correctness guard.** A model-node
     `updates` super-step *has completed* but its `AIMessage` carries `tool_calls` that are
     still **pending** — `handle_update_chunk` emits `TOOL_START` by iterating
     `msg.tool_calls` in *updates* mode (`stream_handlers.py:125-160`), while the tool
     RESULTS (`ToolMessage` → `TOOL_END`) arrive LATER in *messages* mode
     (`handle_message_chunk`, `stream_handlers.py:77-91`, `mtype == "tool"`). Appending a
     `HumanMessage` at the model-node boundary therefore produces
     `AIMessage(tool_calls) → HumanMessage → ToolMessage` on resume (or a dangling
     `tool_use` with no `tool_result`), which violates Anthropic's rule that a `tool_result`
     must immediately follow its `tool_use` — the next model call 400s. We must wait for the
     *tool-node* super-step, whose `updates` boundary leaves the round's `ToolMessage`s as the
     last messages in state (a settled boundary), and inject there.

   ```
   if self.steer_source is not None and not namespace:
       if self._pending_steer is None:
           self._pending_steer = self.steer_source()   # pull once; hold until injected
       if self._pending_steer and await self._steer_boundary_settled():
           steer = self._pending_steer
           # stop the stream cleanly at this settled tool boundary
           await agen.aclose()
           # append the steer as a real user message on THIS thread — safe now:
           # the current tool round's ToolMessages are already in state.
           from langchain_core.messages import HumanMessage
           await self.agent.aupdate_state(
               self._config(), {"messages": [HumanMessage(content=steer)]}
           )
           self._pending_steer = None
           if self.transcript is not None:
               self.transcript.write_user(f"(steered) {steer}", ts=_time.time())
           yield Event(EventKind.NOTICE, text=f"(steered) {steer}",
                       data={"steer": True})
           payload = None          # resume from checkpoint, no new turn payload
           steered = True
           break                   # leave the async-for; re-enter the while-loop
       # else: a steer is held but this boundary is a pending tool round —
       # do NOT consume it; keep _pending_steer and re-check at the next
       # (settled) boundary once the ToolMessages for this round land.
   ```

   `_steer_boundary_settled()` is a new helper that reads state and returns `False` iff the
   last checkpointed message is an `AIMessage` still awaiting its results (mirrors the
   existing `aget_state` read in `_current_turn_index`, `session.py:468-485`):
   ```
   async def _steer_boundary_settled(self) -> bool:
       get_state = getattr(self.agent, "aget_state", None)
       if get_state is None:
           return True                      # fake agent w/o state (tests): treat as settled
       state = await get_state(self._config())
       messages = (getattr(state, "values", {}) or {}).get("messages", []) or []
       if not messages:
           return True
       last = messages[-1]
       # A pending tool round is the ONLY unsafe shape: an AIMessage whose tool_calls
       # have no following ToolMessages yet. (Text-only AIMessage → tool_calls == [] →
       # settled; last is a ToolMessage → settled; last is a HumanMessage → settled.)
       return not (getattr(last, "type", "") == "ai" and getattr(last, "tool_calls", None))
   ```
   (A stricter variant matches each `tool_call.id` to a `ToolMessage.tool_call_id`; the
   last-message check is sufficient because an `AIMessage` with `tool_calls` is only ever the
   *last* message while its round is unexecuted — once the ToolNode runs, its `ToolMessage`s
   are appended after it.)

   Binding the generator is required so it can be closed: replace
   `async for item in self.agent.astream(...)` with `agen = self.agent.astream(...)` +
   `async for item in agen:`, and initialise `steered = False` per while-iteration. Add a
   `_pending_steer: str | None = None` field to `SessionDriver`.

   **Net behavior for the primary use case** ("steer during a long read/think phase"): a
   steer set while the model is emitting a `read_file` tool call is *not* consumed at the
   model-node boundary (pending round → deferred); it is consumed at the following tool-node
   boundary, yielding the valid, provider-accepted sequence
   `AIMessage(read_file) → ToolMessage(contents) → HumanMessage(steer) → AIMessage(next)`.
   The steer still lands before the model's *next* tool call — just after the in-flight
   tool's result, which is exactly right (it never strands a `tool_use`).

3. **Skip DONE / interrupt handling on a steer pass.** After the `async for`, before the
   `if not interrupts:` block (`session.py:389`), add `if steered: continue`. This loops
   straight back to `astream(None, …)` — bypassing both the `DONE` emission and the
   interrupt-resolution path — so the model runs its next super-step having already seen the
   steer as the last human message in state.

4. **`_config()` is the thread anchor.** `aupdate_state` uses `self._config()`
   (`session.py:202`, `{"configurable": {"thread_id": self.thread_id}}`) — the same config
   `astream` runs under — so the append and the resume target the same checkpoint lineage.

**Cancel/append/resume sequence (one steer, arriving during a `read_file` round):**

```
astream(payload) ─▶ updates(model node): AIMessage(read_file) pending  ─▶ steer held
                    _steer_boundary_settled() → False (last msg = AIMessage+tool_calls)
                    → DEFER (keep _pending_steer, keep streaming)
                 ─▶ updates(tool node):  ToolMessage(read_file) now last ─▶ steer applies
                    _steer_boundary_settled() → True
                       await agen.aclose()   (stop stream; round k is durable & settled)
                       await aupdate_state({messages:[HumanMessage("text")]})
                       payload = None ; continue while-loop
astream(None)    ─▶ model node reads state (…, ToolMessage(k), HumanMessage) ─▶ next tool call
```
State order is `AIMessage(read_file) → ToolMessage(contents) → HumanMessage(steer) → …` —
a valid, provider-accepted sequence with no split `tool_use`/`tool_result` pair.

**In-flight tool-result preservation restated:** the break is deferred to a *settled* tool
boundary — a super-step whose `updates` chunk leaves a `ToolMessage` (or a text-only
`AIMessage`/`HumanMessage`) as the last message in state, never an `AIMessage` with pending
`tool_calls`. A ToolNode's results are therefore already checkpointed before we `aclose()`,
and we never split a `tool_use`/`tool_result` pair. A steer that arrives while the model is
streaming a tool call (`messages` mode, or the model-node `updates` boundary) is held in
`_pending_steer` and applied only once that round's `ToolMessage`s land — the model-node
boundary is the **unsafe** seam and is explicitly skipped; the following tool-node boundary
is the safe one.

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

**Transcript / message ordering.** The steer lands as a `HumanMessage` appended at a
*settled* boundary — meaning **all `ToolMessage`s of super-step *k* are already present in
state** (this is the precondition enforced by `_steer_boundary_settled`, §2, and it is
load-bearing, not incidental). The `HumanMessage` is therefore never inserted between an
`AIMessage(tool_calls)` and its `ToolMessage`s. In graph state the order is:
`… → AIMessage(k, tool_calls) → ToolMessage(k) [× each call] → HumanMessage("steer") → AIMessage(k+1)`
(or, for a text-only super-step, `… → AIMessage(k, text) → HumanMessage("steer") → …`). The
model provably sees the steer before its next tool call, and every `tool_use` is immediately
followed by its `tool_result` (this is the §5 T1/T1-companion assertion). In the display
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
   checkpointed message survives an aborted turn today. **Revisited under the §2 gating fix:**
   because a steer is applied only at a *settled* boundary (after a round's `ToolMessage`s
   land), a just-applied-but-unconsumed steer can never orphan a `tool_use` — the persisted
   state is always a valid sequence. That removes the only real hazard, so "leave it" is safe
   and is the recommendation; the §6 open question is downgraded to a cosmetic owner
   preference (strip for tidiness vs. keep visible).

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
  `updates` super-step at a **settled** boundary — a text-only assistant super-step
  `((), "updates", {"model": {"messages": [AIMessage("thinking…")]}})` (no `tool_calls`).
  Call 2 (invoked with `payload is None`) yields the *next* super-step: a tool call, captured
  for the assertion.
- `aget_state(config)` returns `SimpleNamespace(values={"messages": [AIMessage("thinking…")]})`
  (last message has no `tool_calls`) so `_steer_boundary_settled()` is `True` and the steer
  injects at call 1's boundary. (Mirrors `_FakeAgent`, `test_compact.py:18-27`.)
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

### T1-companion — steer WHILE a tool call is pending → valid message sequence (the regression guard)

This is the test that would have caught the Critical: it proves a steer arriving at a
*pending* tool round is deferred to the settled boundary and never splits a
`tool_use`/`tool_result` pair.

*Setup.* A fake agent whose stream and `aget_state` model the real two-mode split:

- `astream` call 1 (`payload == turn`) yields, in order: a main-graph **updates** chunk
  carrying `AIMessage(tool_calls=[{"name":"read_file","id":"c1", "args":{…}}])` (the
  `TOOL_START` seam, pending), then a **messages** chunk
  `((), "messages", (ToolMessage(content="file contents", tool_call_id="c1", name="read_file"),))`
  (main-graph namespace `()`; the `TOOL_END` seam — results land *after* the pending updates
  chunk, exactly as `stream_handlers.py:77-91` vs `:125-160`).
- `aget_state` is **stateful**: it returns `[AIMessage(tool_calls=…)]` (last message pending,
  `_steer_boundary_settled → False`) until `aupdate_state`/the ToolMessage has landed, then
  returns `[AIMessage(tool_calls=…), ToolMessage(c1), …]` (last message a `ToolMessage`,
  settled → `True`). Simplest implementation: the fake tracks a `self._settled` flag flipped
  when the ToolMessage triples have been yielded.
- `astream` call 2 (`payload is None`) yields the next super-step (e.g. a text reply).
- `steer_source` returns `"stop, use the other file"` once.
- Record `self.ops` as in T1.

*Assertions.*
- `_steer_boundary_settled()` returned `False` at the model-node (pending) boundary, so the
  steer was **held, not applied there** — assert no `("update", …)` op precedes the
  `read_file` `TOOL_START`/`TOOL_END` in `self.ops`.
- The `HumanMessage` append (`self.ops` `("update", …)`) occurs **after** the `ToolMessage`
  for `c1` is in state — i.e. the recorded final message order is
  `AIMessage(tool_calls c1) → ToolMessage(c1) → HumanMessage("stop, use the other file")`,
  with no `HumanMessage` between the `AIMessage` and its `ToolMessage`.
- A validity assertion that generalizes the rule: for every `AIMessage` in the resulting
  message list that has `tool_calls`, each `tool_call.id` has an immediately-following
  `ToolMessage` with matching `tool_call_id` (no orphaned `tool_use`) — this is the precise
  provider-acceptability check.

### T2 — abort during a pending steer

*Setup.* A `Hang`-style agent (`test_session_stream.py:378`) whose call-1 `astream` yields
one main-graph `updates` chunk (settled: `aget_state` returns a last message with no pending
`tool_calls`, so the steer applies at that boundary) then `await asyncio.sleep(10)` on call 2
(holds the turn open). `steer_source` returns text once. `aupdate_state` records into
`self.updated`.

*Drive.* Start the turn as a task, advance it past the first `updates` boundary (so the
steer is applied and the resume `astream(None)` is hanging), then cancel:
`agen = driver.run_turn(...)`, pump `__anext__()` until the steer NOTICE is seen, then
`await agen.aclose()` (mirrors `test_snapshot_task_not_leaked_on_cancelled_turn`,
`test_session_stream.py:354`).

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

1. **[TOP RISK] The injected `HumanMessage` must NEVER land between an `AIMessage(tool_calls)`
   and its `ToolMessage`s — verify against the real `AsyncSqliteSaver`.** The fixture tests
   use fake agents; they cannot prove that, against the real async SQLite checkpointer
   (`create_async_checkpointer`, `sessions.py:89`): (a) at a settled boundary `aget_state`
   actually reflects the just-landed `ToolMessage`s (so `_steer_boundary_settled` is reading
   truth, not a stale checkpoint); (b) `aclose()` leaves a *clean* checkpoint (no half-written
   super-step); and (c) `aupdate_state` then `astream(None)` on the same `thread_id` neither
   deadlocks the single sqlite connection nor mis-orders messages. **The pass/fail assertion
   is the tool_use/tool_result adjacency invariant:** in the resulting checkpointed message
   list, every `AIMessage.tool_calls[i].id` is immediately followed by a `ToolMessage` with
   the matching `tool_call_id`, and no `HumanMessage` ever separates them. T-4-6 must add a
   real-checkpointer integration check (a temp `state.sqlite`, a trivial model→tools graph
   that emits a real tool round) that (i) steers *during* the pending round and (ii) asserts
   this invariant + a clean resume. This is the single most important thing to validate
   before shipping — it is the exact defect the design review caught in the first draft.

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
   **Important — this does NOT solve the Critical:** folding append+resume into one
   `Command(update=…)` call only removes the abort race window; it still inserts the
   `HumanMessage` at *whatever* boundary it is invoked. If invoked at a pending tool round it
   produces the same `AIMessage(tool_calls) → HumanMessage → ToolMessage` violation. The
   settled-boundary gate (`_steer_boundary_settled`, §2) is what fixes the Critical and is
   required regardless of which of the two append forms is chosen.

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
  - Add `steer_source: Callable[[], str | None] | None = None` and
    `_pending_steer: str | None = None` fields to `SessionDriver`.
  - Add the `_steer_boundary_settled()` helper (reads `aget_state`; `False` iff the last
    message is an `AIMessage` with unfulfilled `tool_calls` — §2).
  - `_stream_turn` (`session.py:307`): bind the `astream` generator; add the main-graph
    (`not namespace`) **settled-boundary** steer check — pull-and-hold into `_pending_steer`,
    inject only when `_steer_boundary_settled()` is `True`: `aclose` → `aupdate_state` →
    clear `_pending_steer` → `payload=None` → `steered=True; break`; add `if steered: continue`
    before the `if not interrupts:` block. Defer (keep `_pending_steer`, keep streaming) when
    the boundary is a pending tool round.
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
- Tests: T1, **T1-companion (steer while a tool call is pending — the regression guard)**,
  T2, T3 (§5) + the real-checkpointer integration check (risk 1).

**Explicit DoD carry-over:** T-3-8 delivers this spec with a chosen mechanism (**(a)
cooperative checkpoint**) and a test plan; T-4-6 is thereby unblocked.
