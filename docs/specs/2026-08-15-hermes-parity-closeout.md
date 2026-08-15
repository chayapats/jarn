# Hermes display-parity close-out

- **Status:** planned / implementing
- **Goal:** Hermes-equivalent **display and command muscle memory** on the CLI
  and Telegram. Identity stays cyan/teal. Host-direct `!` stays red.
- **Not a product clone:** scanability, quiet defaults, and command names a
  Hermes user already reaches for — not skins, voice, pets, or a second TUI.
- **Surfaces:** interactive REPL, slash-command catalog, Telegram DM gateway.
- **Predecessor:** [2026-08-15-hermes-aligned-display-standard.md](2026-08-15-hermes-aligned-display-standard.md)
  (waves A–K landed on main via
  [#92](https://github.com/chayapats/jarn/pull/92)). That file remains the
  visual-grammar SSOT; **this file is the SSOT for remaining work** until CLI +
  Telegram match the gates in §2.
- **P0-1 note:** do not rewrite the predecessor spec, `docs/ROADMAP.md`, or
  Python in this task. P0-2 / P0-3 / P0-4 retarget those docs afterward.

---

## 1. Why this work exists

Waves A–K shipped one visual grammar: palette, `tui/layout.py`, grouped
`/help`, splash info strip, toolbar fill bar + duration + sticky YOLO, quiet
tool stream (`ui.tool_progress` + `/verbose` + `/focus`), `/status` recap,
`/context` `/tools` `/title` `/usage`, grouped `jarn --help`, and Telegram HTML
for readonly pages.

The *grammar* is in tree. The *parity* is not:

- Several Hermes-shaped CLI commands and chrome pieces from wave F never
  landed (`/diff`, `/busy`, paste preview, sessions picker, compact-count,
  resume recap, background finish panel).
- Telegram `/verbose` is a no-op on the live turn: `should_drop_event` still
  hard-drops every tool event (#40).
- Telegram local slash coverage is readonly pages plus `/verbose` `/focus`
  `/title`. `/model` `/mode` `/compact` `/undo` are not local. Skill slash
  `seed_turn` is discarded. Mutating names such as `/config set` fall through
  to `submit_turn` and become agent turns.

This close-out finishes display and command muscle memory. It does not reopen
the permission engine, the danger-guard, or remote ALWAYS.

---

## 2. Definition of equivalent

The whole program is done when every gate below is true. These are acceptance
for the close-out, not a new visual language.

- **CLI launch:** model, folder, mode, skills without `/status`. Toolbar shows
  pressure and cost. YOLO cannot hide.
- **CLI turn:** default `new` one ⏺/⎿ per tool. `/focus` hides chrome.
  `/verbose` cycles. Paste of many lines shows one preview line.
- **CLI commands:** `/help` finds `/mode`. `/HELP` works. `/sessions` filters.
  `/diff` and `/busy` exist. `/usage` aliases `/cost`.
- **Telegram quiet:** default turn is prose draft → finalize only. Working
  heartbeat after N quiet minutes.
- **Telegram opt-in:** `/verbose` then a turn shows one edited progress
  bubble; it disappears when the answer lands.
- **Telegram commands:** `/status` `/help` `/model` `/mode` `/compact` `/undo`
  `/sessions` `/resume <id>` `/title` work locally. `/config set` is refused
  with a terminal hint.
- **Safety unchanged:** no remote ALWAYS. YOLO still a card. Untrusted still
  clamped. `!` still host-direct and red.

J.A.R.N. still looks like J.A.R.N.: cyan/teal accent, permission glyphs, red
host-direct `!`, native scrollback. Hermes users should not need a second
manual for “where am I?”, “what is the model doing?”, or “how do I switch
mode?”.

---

## 3. Already landed (do not re-implement)

Visual grammar SSOT from waves A–K on main (#92). Callers already exist; this
close-out consumes them.

| Area | In tree |
|---|---|
| Palette + context ramp 50/80/95 | `src/jarn/tui/palette.py`, `tui/grammar.py` |
| Layout helpers (Rich + Telegram HTML) | `src/jarn/tui/layout.py` |
| Grouped `/help`, `/help <name>`, case-insensitive names | `commands/registry.py`, `commands/help.py` |
| Skills as slash commands (REPL) | `Controller.handle_command` → `cmd_skill` |
| Splash info strip | `src/jarn/tui/logo.py` |
| Toolbar fill bar + duration + YOLO sticky | `src/jarn/tui/toolbar.py` |
| `ui.tool_progress` + `/verbose` + `/focus` | `repl_renderer.py`, `diagnostics.cmd_verbose` / `cmd_focus` |
| `/status` recap (local, no LLM) | `diagnostics._status_recap` / `_transcript_recap` |
| `/context` `/tools` `/title` `/usage` | registry + diagnostics |
| Grouped `jarn --help` | `src/jarn/cli.py` |
| Telegram HTML dialect for readonly pages | `layout.py` `dialect="html"`, worker `_try_local_slash` |
| Gateway command layers (partial) | `GATEWAY_READONLY_COMMANDS` ∪ `GATEWAY_SESSION_COMMANDS` = `GATEWAY_LOCAL_COMMANDS` |

CLI launch gates (splash + toolbar pressure/cost + sticky YOLO) and the quiet
default tool stream are **already** the A–K contract. P1 does not rebuild them.
P5 UAT re-checks them.

`/usage` already aliases `/cost` (`alias_of="cost"`). Do not add a second
spelling.

---

## 4. Remaining gaps (grounded in code)

### 4.1 CLI

| Gap | Evidence |
|---|---|
| Paste preview | `repl/keys.py` `_paste` already collapses bracketed paste (≥3 newlines or >800 chars) to `[Pasted text #N +L lines]` in the **input buffer**, restored via `_expand_pastes`. Submitted echo is still `layout.prompt(stripped)` — not a dim one-line preview in scrollback. |
| Resume recap panel | `_resume_picker` (`repl/commands.py`) resumes + replays transcript. `/status` already has a local recap; resume does not print it. |
| Compact-count + title on toolbar | `render_toolbar` has model, mode, YOLO, cwd, provider, trust, queue, context bar, duration, cost. No compact-count. No session title. No `compact_count` field on `Controller`. |
| `/sessions` as picker | `cmd_sessions` is a text list (“use /resume to pick one”). Interactive picker is `/resume` only (`layer="ui"`). No query filter. |
| `/diff` | No slash command. `tui/widgets/diff.py` already renders approval diffs. |
| `/busy` | No command. Busy lines go to `InputQueue`; `[s]` / `/queue steer <n>` already inject via `controller._steer_slot`. `ui.steering` exists. No `ui.busy_input_mode`. |
| Markdown-light on dumb/`NO_COLOR` | Rich `Markdown` in `repl/app.py` / `repl/turn.py`. `palette.no_color()` strips styles; wrapper `**` can still leak on dumb/`NO_COLOR` if Markdown is skipped or printed raw. |
| Background finish panel | `run_in_background` / `/ps` exist. No scrollback panel when a job exits. |

### 4.2 Telegram live turn

| Gap | Evidence |
|---|---|
| `/verbose` is a no-op on the stream | `should_drop_event` (`telegram/outbox.py`) returns True for every `tool_start` / `tool_end` / `tool_progress` / `tool_call`. Subagent inner stream (`data["agent"]`) is also dropped — **keep that**. |
| Worker already forwards tool events | `event_to_frame` copies `event.kind` including `tool_progress`. The outbox throws them away. |
| No accumulate bubble | `TelegramSender` is draft + `send_message` only. Prose uses `sendMessageDraft` → `sendMessage`. No `editMessageText` progress message. |
| No long-running heartbeat | `keepalive_draft` only resets the ~30s draft TTL. There is no user-visible “Working — N min”. `GatewayTelegramConfig` is `token` + `allowed_user_ids` only. |
| No cleanup delete | No `delete_message` on the sender protocol. Finalize persists the prose draft and does not remove a progress bubble (there isn’t one). |

Default quiet turn (draft → finalize, tools off) is **intentional** (#40) and
stays the default. The gap is that opt-in `/verbose` cannot turn tools on.

### 4.3 Telegram commands

| Gap | Evidence |
|---|---|
| `GATEWAY_LOCAL` is readonly + session chrome only | Readonly pages + `verbose` / `focus` / `title`. Not in the set: `model`, `mode`, `compact`, `undo`, `redo`, `resume`, `skill`. |
| Skill slash `seed_turn` discarded | `_try_local_slash`: `if result.seed_turn: return False`. REPL routes `seed_turn` into `_run_turn`. Worker then sends the raw slash line to the agent (or never handles `/skill` at all, because `skill` is not local). |
| `/model` `/mode` not local | They fall through `bot.py` → `submit_turn` (agent turn) unless intercepted. Sync `cmd_mode` **refuses** trusted YOLO escalate (T-CTRL-1 / #59) and tells the caller to use `set_permission_mode(..., confirm=…)`. Telegram already has `build_yolo_confirm_card` / `send_yolo_confirm`. |
| `/compact` `/undo` not local | `cmd_compact` with no args is a **status page**; real compact is REPL `await controller.compact()`. `cmd_undo` is **preview-only**; mutate path is `await controller.undo(confirm=…)`. |
| Mutating names become agent turns | `bot.py` intercepts only `/stop` `/new` `/repo` `/help`, then `submit_turn`. `/config set` is therefore a user prompt to the model. |
| `rebuilt=True` ignored | Worker local path emits `notice` + `done`. REPL calls `_invalidate_runtime()` when `result.rebuilt`. |
| No BotFather menu | No `setMyCommands` in `telegram/`. |

---

## 5. Work queue

Each task has an ID, intent, primary files, tests, and a one-line acceptance.
Implement in PR sequence §8. Do not mix a renderer rewrite with a command-layer
rewrite.

### P0 — Contract (docs only)

| ID | Intent | Primary files | Tests | Acceptance |
|---|---|---|---|---|
| **P0-1** | This spec (SSOT for remaining work). | `docs/specs/2026-08-15-hermes-parity-closeout.md`; link from `docs/README.md` | none | A later agent can implement P1-1 from this file alone. |
| **P0-2** | Mark the display-standard spec **landed**; point remaining work here. | `docs/specs/2026-08-15-hermes-aligned-display-standard.md` | none | Status no longer says “implementing”; remaining wave F items point at this close-out. |
| **P0-3** | Tick ROADMAP waves A–K shipped; add this close-out as the next planned block; date-stamp. | `docs/ROADMAP.md` | none | A–K checked; close-out listed; date updated. |
| **P0-4** | Extend the gateway parity record with a v1.1 display-parity table. | `docs/TELEGRAM_GATEWAY_PARITY.md` | none | New table rows for quiet default, opt-in verbose bubble, local command set, mutating refuse. No unmarked Partial. |
| **P0-5** | Echo §11 non-goals in ROADMAP / parity docs. Non-goals are already frozen **here**. | `docs/ROADMAP.md`, `docs/TELEGRAM_GATEWAY_PARITY.md` | none | Those docs do not promise voice, groups, other platforms, skins, second TUI, remote ALWAYS, or `!` through approvals. |

P0 writes no Python.

### P1 — CLI remaining

#### P1-1 Paste preview

- **Intent:** Multiline paste collapses to **one dim preview line** in
  scrollback. The agent still receives the full text.
- **Do not replace** the existing `_pastes` / `_expand_pastes` /
  `_paste` path in `repl/keys.py`. Keep the bracketed-paste token in the input
  buffer. Change the **submitted echo**: if the expanded payload is multiline
  (or already a paste token), print one dim line (`layout.muted` / a small
  `layout` helper), not `layout.prompt` of a wall of lines.
- **Primary files:** `src/jarn/repl/keys.py`, `src/jarn/repl/app.py`; optional
  helper in `src/jarn/tui/layout.py`.
- **Tests:** `tests/test_repl_paste.py` (extend the placeholder round-trip;
  assert the echo path is one line).
- **Acceptance:** Paste of many lines shows one preview line; submit still
  expands to the original payload.

#### P1-2 Resume recap panel

- **Intent:** After `/resume`, `--resume` picker, or continue-into-a-thread,
  print the same **local** recap `/status` already builds. No LLM.
- **Reuse:** `diagnostics._status_recap` / `_transcript_recap` (or a thin
  wrapper they both call). Do not duplicate transcript scanning.
- **Primary files:** `src/jarn/repl/commands.py` (`_resume_picker`),
  `src/jarn/controller/commands/diagnostics.py`, maybe `repl/app.py` for
  `_resume_on_start`.
- **Tests:** `tests/test_status_recap.py`; a resume-path test that the recap
  lines appear with no model call.
- **Acceptance:** Resume or continue shows directory/model/mode/last-turn
  recap without calling a model.

#### P1-3 Toolbar: compact-count + title

- **Intent:** Compact-count badge; session title pinned **right**. Drop
  priority among the named pieces: **YOLO > model > bar > title** (YOLO never
  drops before model; title drops first).
- **Compact count:** session-local counter on `Controller`, incremented in
  `compact_apply`. If auto-compact can be observed without a new engine API,
  count it too; otherwise count `compact_apply` only and say so in `/status`.
  Not a YAML key.
- **Title:** `SessionIndex` already stores `title` (`/title`, first prompt).
  Pass it into `render_toolbar` as a new optional kwarg. High `order` (right),
  low keep-priority (dropped before the fill bar).
- **Primary files:** `src/jarn/tui/toolbar.py`, `src/jarn/repl/app.py` (toolbar
  assembler), `src/jarn/controller/core.py` (`compact_apply`).
- **Tests:** toolbar cases in `tests/test_phase3.py` (there is no
  `test_toolbar.py`). Widths 40 / 60 / 80 / 120; YOLO still present in yolo
  mode; title absent on the narrow fixture; compact badge present when count
  > 0 on a wide fixture.
- **Acceptance:** Wide toolbar shows compact-count and title; YOLO still
  cannot hide; title is the first of {YOLO, model, bar, title} to drop.

#### P1-4 `/sessions` picker

- **Intent:** `/sessions` becomes the interactive picker; `/resume` aliases
  it. Text list still works for Telegram (and any non-TTY caller of
  `cmd_sessions`).
- **Filter:** `/sessions [q]` filters `session_label` / title / thread-id
  prefix (CLI gate: “`/sessions` filters”). Empty query = full list / picker.
- **Telegram:** keep `cmd_sessions` as the HTML/text list; `/resume <id>` is
  P3-3. Do not open a prompt_toolkit overlay on the gateway.
- **Primary files:** `src/jarn/commands/registry.py` (alias),
  `src/jarn/repl/commands.py`, `src/jarn/controller/commands/session.py`.
- **Tests:** picker still resumes; `cmd_sessions` with a query filters; README
  parity if the index line changes (`test_phase3.py`).
- **Acceptance:** REPL `/sessions` is the picker; `/resume` is the same;
  Telegram still gets a text list.

#### P1-5 `/diff [staged|all|session]`

- **Intent:** Slash command over existing `tui/widgets/diff.py` (and git /
  checkpoint). `staged` = index vs HEAD; `all` = working tree; `session` =
  files this thread touched when that is cheap from checkpoints / recap
  files. Default should be the smallest useful view (`staged` if dirty index,
  else `all`).
- **Primary files:** new handler under `controller/commands/` or
  `repl/commands.py`, `src/jarn/tui/widgets/diff.py`, `commands/registry.py`.
- **Tests:** `tests/test_diff_render.py` plus a command test with a tiny git
  repo fixture.
- **Acceptance:** `/diff` prints a colored unified diff; unknown subcommand
  uses `usage_error("diff")`.

#### P1-6 `/busy [interrupt|queue|steer|status]`

- **Intent:** Wrap existing queue / steer / abort. Do **not** add a second
  queue. Persist **only** via `/config set ui.busy_input_mode` — `/busy`
  itself is session-only, same rule as `/verbose`.
- **Modes:** `status` prints the current mode. `queue` = today’s
  `InputQueue`. `steer` = `/queue steer` / `_steer_slot` (requires
  `ui.steering`). `interrupt` = `controller.abort()` then run the line (reuse
  abort; no new engine interrupt).
- **Default:** CLI stays `queue` (current Enter-while-busy).
- **Primary files:** `commands/registry.py`, `repl/commands.py` /
  `repl/keys.py`, `config/schema.py` + pydantic + defaults + SETTINGS.
- **Tests:** `/busy status`; session change does not write YAML; `/config set
  ui.busy_input_mode steer` persists.
- **Acceptance:** `/busy` exists; YAML changes only through `/config set
  ui.busy_input_mode`.

#### P1-7 Markdown-light audit (`NO_COLOR`)

- **Intent:** Audit the **final** committed assistant markdown. Strip
  wrapper markup (`**`, `__`) **only** on dumb/`NO_COLOR` if those wrappers
  leak as literals. Keep fences and lists.
- **Primary files:** `src/jarn/repl/app.py`, `src/jarn/repl/turn.py`, maybe
  `tui/layout.py`.
- **Tests:** `tests/test_terminal_contract.py`, `tests/test_ux.py` — zero CSI
  on `NO_COLOR`; a fixture whose source contains `**bold**` does not print
  raw asterisks on `TERM=dumb`.
- **Acceptance:** dumb/`NO_COLOR` final answer has no leaked `**` wrappers;
  TTY Rich markdown unchanged.

#### P1-8 Background finish panel

- **Intent:** When a `run_in_background` job exits, print a small panel in
  scrollback (id, exit code, one-line tail, `/ps` hint). Do not invent a
  parallel job registry — hook `jarn.agent.background`.
- **Primary files:** `src/jarn/agent/background.py`, `src/jarn/repl/app.py` or
  turn loop, `tui/layout.py` for the panel.
- **Tests:** `tests/test_background.py`.
- **Acceptance:** A finished background job appears in scrollback without
  `/ps`.

#### P1-9 Tests + README command table

- **Intent:** Registry remains the catalog. New `/diff` `/busy` (and any
  `/sessions` index change) flow through `readme_command_rows()`.
- **Primary files:** `README.md`, `README-TH.md`, `commands/registry.py`.
- **Tests:** `tests/test_phase3.py` README parity; `tests/test_repl.py` help
  body from registry.
- **Acceptance:** README command table matches the registry; pytest green
  for P1.

### P2 — Telegram live turn

Default remains **off**. `#40` draft → finalize for prose is unchanged.

#### P2-1 `should_drop_event` honors progress

- **Intent:** Drop tool events unless the effective Telegram progress is
  `new` / `all` / `verbose`. Default argument / default session value stays
  `off` so existing tests keep passing.
- **Signature:** extend `should_drop_event(kind, data=None, *, progress="off")`
  (or pass progress on `Outbox`). Do not drop `text` / `done` / cards.
  **Always** drop `data.get("agent")` (subagent inner stream).
- **Primary files:** `src/jarn/telegram/outbox.py`.
- **Tests:** `tests/test_telegram_outbox.py` — off still drops tools; `new`
  does not drop `tool_start`/`tool_end`; subagent still drops.
- **Acceptance:** `/verbose` can make tool events visible; default still
  quiet.

#### P2-2 Worker forwards `controller.tool_progress`

- **Intent:** Outbox uses the worker controller’s session `tool_progress`
  (after `/verbose` / `/focus`). `/verbose` / `/focus` already mutate
  `ctrl.tool_progress` and must **not** write YAML (already true).
- **Primary files:** `src/jarn/gateway/worker.py`, `src/jarn/telegram/outbox.py`
  (or the bot event pump that calls `on_event`).
- **Tests:** worker/outbox: `/verbose` then `tool_start` is not dropped.
- **Acceptance:** Next tool line after `/verbose` / `/focus` follows the
  session density.

#### P2-3 Accumulate: one edit-in-place HTML bubble

- **Intent:** One progress message, edited in place (`editMessageText`),
  HTML dialect via `layout.py`. Not `sendMessageDraft` — that channel stays
  the prose draft.
- **Extend** `TelegramSender` with `edit_message` (aiogram
  `edit_message_text`). Do not overload `send_message_draft`.
- **Density:** `new` = one ⏺/⎿ style line per distinct tool (mirror CLI
  `new`). Do not dump verbose tails into Telegram by default even if CLI
  `ui.tool_progress` is `new` in YAML — Telegram starts `off` until overlay
  or `/verbose` (P2-6).
- **Primary files:** `src/jarn/telegram/outbox.py`, sender adapter.
- **Tests:** `test_telegram_outbox.py` — one message id edited; no second
  progress message.
- **Acceptance:** `/verbose` then a turn shows one edited progress bubble.

#### P2-4 Long-running heartbeat

- **Intent:** After N quiet minutes with a turn in flight, show
  `Working — N min`. Gate:
  `gateway.telegram.long_running_notifications` (bool, **default true**).
  Quiet interval is a code constant on the order of minutes (not
  `ui.notify_min_secs`, which is 10s for the CLI bell). Do not conflate with
  `keepalive_draft` (draft TTL).
- **Primary files:** `outbox.py` or bot loop, `config/schema.py` + pydantic +
  defaults.
- **Tests:** fake clock: no heartbeat before the interval; one heartbeat
  after; disabled when the flag is false.
- **Acceptance:** Quiet long turn surfaces Working — N min unless the flag
  is off.

#### P2-5 Cleanup delete

- **Intent:** After finalize, delete the progress bubble when
  `gateway.telegram.tool_progress_cleanup` is `delete` (default). `keep`
  leaves it. Prose draft finalize is unchanged.
- **Extend** `TelegramSender` with `delete_message`.
- **Primary files:** `outbox.py`, config schema.
- **Tests:** `verbose` then `done` → delete called; `keep` → not deleted.
- **Acceptance:** Progress bubble disappears when the answer lands
  (`delete`).

#### P2-6 Config overlay

- **Intent:**
  - CLI: existing `ui.tool_progress` (`off|new|all|verbose`, default `new`).
  - Telegram: `gateway.telegram.tool_progress` overlay (`off|new|all|verbose`
    or unset). Unset → session starts **off**, not the CLI YAML default.
  - `/verbose` cycles session state only; never writes YAML.
- **Primary files:** `schema.py`, `pydantic_schema.py`, `defaults.py`,
  `docs/CONFIGURATION.md` (P5-5 can land the user-facing table; schema must
  ship with the consumer).
- **Tests:** config validation; overlay unset does not inherit `ui.tool_progress:
  new`.
- **Acceptance:** Telegram stays quiet until `/verbose` or an explicit
  overlay; `/verbose` does not write YAML.

#### P2-7 Isolation rules

- **Intent:** Subagent inner stream still dropped. Approval cards still
  `restart_draft` (destroy prose draft). Progress bubble is **separate** from
  the prose draft — a card must not be mistaken for the progress message, and
  deleting progress must not delete the finalized answer.
- **Primary files:** `outbox.py`.
- **Tests:** tool event with `data.agent` dropped even when progress is
  `new`; approval_ask still kills draft; progress `message_id` ≠ draft id.
- **Acceptance:** Cards and subagents behave as #40; verbose bubble is a
  third channel.

#### P2-8 Telegram live tests

- **Intent:** Cover off / new / verbose / cleanup / verbose-then-tool_start.
- **Primary files:** `tests/test_telegram_outbox.py` (and worker tests if the
  pump lives there).
- **Acceptance:** Those five cases are pytest, not only UAT.

### P3 — Telegram commands

#### P3-1 Registry layers

- **Intent:** Three layers in `commands/registry.py`:
  - `GATEWAY_READONLY_COMMANDS` — display pages (keep the current set unless
    a name must move).
  - `GATEWAY_SESSION_COMMANDS` — session chrome + local session mutations
    that already have controller APIs. Today: `verbose`, `focus`, `title`.
    P3-2/P3-3/P3-4 add `model`, `mode`, `compact`, `undo`, `redo`, `resume`,
    `skill` (see those tasks).
  - `GATEWAY_MUTATING_COMMANDS` — **blocked**: never local `handle_command`
    as the mutate path, never `submit_turn`. Closed set from the catalog,
    including at least: `config`, `preset`, `memory`, `sandbox`, `trust`,
    `key`, `login`, `logout`, `add-dir`, `init`, `module`, `theme`, `rewind`,
    `queue`, `abort`, `commit`, `review`, `clear`, `quit`, `exit`, `expand`.
- **Rule:** a leading slash that matches a builtin is local, gateway-only,
  or refused. It is never an agent prompt.
- **Keep** `GATEWAY_LOCAL_COMMANDS = READONLY | SESSION` and
  `is_gateway_local_command`.
- **Primary files:** `src/jarn/commands/registry.py`.
- **Tests:** `tests/test_layout.py` gateway-local assertions; mutating names
  not in `GATEWAY_LOCAL_COMMANDS`.
- **Acceptance:** Layers exist; `config` is blocked; `status` is local.

#### P3-2 `/model` `/mode` local + YOLO card

- **Intent:** `/model` and `/mode` run locally. Trusted YOLO escalate **must**
  open the existing yolo confirm card (`send_yolo_confirm` /
  `build_yolo_confirm_card`). **Never** silent `handle_command("mode", "yolo")`
  — that path already refuses (T-CTRL-1). Use
  `await controller.set_permission_mode(..., confirm=…)`. Untrusted still
  clamps via `apply_mode`. `/model` with no args: current model kv page (sync
  `cmd_model` already does this); with a name: set + `rebuilt=True`.
- **Primary files:** `gateway/worker.py`, `telegram/bot.py` or backend,
  `controller/async_ops.py` (existing).
- **Tests:** yolo card shown; cancel leaves mode unchanged; untrusted clamp
  notice; no agent turn for `/mode ask`.
- **Acceptance:** `/model` `/mode` work locally; YOLO is a card.

#### P3-3 Text fallbacks

- **Intent:** `/sessions [q]` `/resume <id>` `/checkpoints` `/undo` `/redo`
  `/compact` `/ps` work without pickers.
  - `/sessions` `/checkpoints` `/ps` already readonly local — add query
    filter on `/sessions`; `/ps kill` already in usage.
  - `/resume <id>`: `controller.resume_thread(id)` + notice; missing id →
    usage / unknown session. No overlay.
  - `/compact`: `await controller.compact()` (not the status-only sync
    handler). `/compact status` may stay `handle_command`.
  - `/undo` `/redo`: `await controller.undo(confirm=…)` /
    `redo()`. Confirm via a small Confirm/Cancel card (same callback
    pattern as yolo) or an explicit `/undo confirm` after the existing
    preview. Do not treat sync `cmd_undo` as the mutate path.
- **Primary files:** `gateway/worker.py`, `controller/commands/session.py`,
  `telegram/outbox.py` if a confirm card is reused.
- **Tests:** each name returns a notice/HTML page; `/resume` unknown id does
  not start an agent turn; undo without confirm does not restore.
- **Acceptance:** Those commands work from Telegram as text.

#### P3-4 Skill slash → `seed_turn`

- **Intent:** `/skill <name>` and `/skill-name` call `cmd_skill`, then inject
  `seed_input` into the **same** worker turn (`run_agent_turn`), matching
  REPL `repl/commands.py`. Stop discarding `result.seed_turn` in
  `_try_local_slash`.
- **Primary files:** `src/jarn/gateway/worker.py`, `controller/commands/meta.py`
  (already returns `seed_turn=True`).
- **Tests:** `test_gateway_worker.py` — `/skill` does not emit only notice;
  `run_agent_turn` receives `seed_input`; unknown skill is a notice, not an
  agent prompt.
- **Acceptance:** Skill slash seeds and runs the agent in that turn.

#### P3-5 Gateway-only names

- **Intent:** Keep bot-layer `/stop` `/new` `/repo` `/help`. `/reset` aliases
  `/new` (gateway thread, not REPL `/clear`). `/rollback` is a **help alias**
  pointing at `/checkpoints` + `/undo` — not a new mutate command.
- **Primary files:** `src/jarn/telegram/bot.py` (`_GATEWAY_HELP_ROWS`).
- **Tests:** `tests/test_telegram_bot.py` — `/reset` = `/new`; help lists
  rollback as alias copy.
- **Acceptance:** `/reset` starts a fresh thread; help mentions rollback.

#### P3-6 `/help` HTML + Gateway section

- **Intent:** Keep layout HTML + Gateway section. Chunk with `chunk_html`
  at Telegram’s 4096 cap (`TELEGRAM_MESSAGE_MAX` in `htmlutil.py`; helper
  already exists). Bot-intercepted `/help` must chunk; do not send one
  oversized message.
- **Primary files:** `telegram/bot.py`, `telegram/htmlutil.py`.
- **Tests:** long help splits; tags not split mid-entity (existing
  `chunk_html` tests + bot send).
- **Acceptance:** `/help` fits Telegram; Gateway section present.

#### P3-7 Mutating names → terminal hint

- **Intent:** `/config set` (and every `GATEWAY_MUTATING_COMMANDS` name) is
  refused with one line: use the terminal / `jarn` CLI. Do **not** route as
  agent turns. Intercept in `bot.py` or worker **before** `submit_turn`.
- **Primary files:** `telegram/bot.py`, `gateway/worker.py`, `registry.py`.
- **Tests:** `/config set ui.theme light` is a notice, zero `submit_turn`.
- **Acceptance:** `/config set` is refused with a terminal hint.

#### P3-8 BotFather command menu

- **Intent:** `setMyCommands` at bot start from `GATEWAY_LOCAL` names +
  gateway-only (`stop`, `new`, `repo`, `help`, `reset`). No mutating names.
- **Primary files:** `telegram/bot.py` or `telegram/setup.py`.
- **Tests:** menu names ⊆ allowed set.
- **Acceptance:** Bot command list matches local + gateway-only names.

#### P3-9 Worker `rebuilt` + `seed_turn`

- **Intent:** `rebuilt=True` → notice + `controller._invalidate_runtime()`
  (same generation-bump as REPL). `seed_turn` → inject `seed_input` and run
  the agent (P3-4).
- **Primary files:** `src/jarn/gateway/worker.py`.
- **Tests:** `test_gateway_worker.py`.
- **Acceptance:** Local `/model name` rebuilds; skill slash runs the agent.

#### P3-10 Command-layer tests

- **Intent:** Registry layers, YOLO card, mutating refuse, skill seed,
  `/resume <id>`, `/compact`, `/undo` confirm gate, help chunking.
- **Primary files:** `tests/test_layout.py`, `tests/test_gateway_worker.py`,
  `tests/test_telegram_bot.py`, `tests/test_telegram_outbox.py`.
- **Acceptance:** P3 behavior is pytest-covered.

### P4 — Shared busy UX

#### P4-1 CLI `/busy` wraps queue/steer

- **Intent:** P1-6’s `/busy` is the CLI wrapper. This task wires Enter-while-busy
  to the session mode (`queue` / `steer` / `interrupt`) without a second
  `InputQueue`.
- **Primary files:** `src/jarn/repl/keys.py`.
- **Tests:** `tests/test_repl.py` busy-enter paths.
- **Acceptance:** `/busy steer` then Enter during a turn steers; `/busy queue`
  queues.

#### P4-2 Telegram second text = steer or queue

- **Intent:** A second DM during a turn is **steer** (default) or **queue**.
  Never a second `_run_turn`. Today `_handle_turn` emits `ErrorFrame`
  `code="busy"` and returns — replace that with `SteerFrame` /
  `_handle_steer` / `_steer_slot` (already on the worker) or a one-line
  queue drained after `done`. Keep the T-QA-1 re-entrancy guard: one in-flight
  turn task.
- **Telegram default is steer**, even if CLI `ui.busy_input_mode` is `queue`.
  Optional overlay `gateway.telegram.busy_input_mode` if a shared key would
  otherwise inherit CLI queue.
- **Primary files:** `gateway/worker.py`, `telegram/bot.py` / `backend.py`,
  `gateway/protocol.py` (`SteerFrame` exists).
- **Tests:** `test_gateway_worker.py` — second `TurnFrame` while in flight
  does not start another `_run_turn`; steer slot is set.
- **Acceptance:** Second text never double-runs the agent.

#### P4-3 Busy ack

- **Intent:** One short `Working…` edit as ack. `ui.busy_ack_detail` default
  **off** (no extra “queued/steering” paragraph unless enabled).
- **Primary files:** `telegram/outbox.py`, config schema.
- **Tests:** one short ack; detail off by default.
- **Acceptance:** Busy DM gets one short Working…; no essay.

#### P4-4 Optional `/background`

- **Intent:** Thin slash wrapper over existing background tools + `/ps`.
  Not a blocker if `/ps` + tools already cover the job. If added, registry +
  README parity.
- **Primary files:** `commands/registry.py`, diagnostics `/ps` handler.
- **Tests:** `test_background.py` / phase3 parity if the command is added.
- **Acceptance:** Either `/background` wraps `/ps` or the spec’s optional
  box is checked “deferred — `/ps` sufficient” in P5-6.

#### P4-5 Ctrl+S stash (optional, not a blocker)

- **Intent:** Hermes Ctrl+S input stash is **not** required to close this
  program. If cheap against the existing input buffer, add it; otherwise
  document skipped in P5-6.
- **Primary files:** `repl/keys.py` only if implemented.
- **Tests:** only if implemented.
- **Acceptance:** Program can ship without Ctrl+S stash.

#### P4-6 Busy tests

- **Intent:** CLI mode switch + Telegram second-text steer/queue + no double
  `_run_turn`.
- **Primary files:** `tests/test_repl.py`, `tests/test_gateway_worker.py`.
- **Acceptance:** P4 pytest covers both surfaces.

### P5 — Evidence

| ID | Intent | Primary files | Tests | Acceptance |
|---|---|---|---|---|
| **P5-1** | CLI UAT: 80x24, 120x40, `NO_COLOR`, light theme, YOLO, paste, `/sessions`. | notes under `docs/assets/` or the PR body | manual | Checklist filled. |
| **P5-2** | Telegram UAT: `/status` `/help` `/cost` `/verbose`+turn `/model` yolo card skill slash. | PR body | manual | Checklist filled. |
| **P5-3** | `demo.gif` via `scripts/record-demo.sh` if `vhs` is available. **Do not fail the program if vhs is missing — document.** | `scripts/record-demo.sh`, `docs/assets/README.md` | none | GIF committed **or** explicit “vhs not installed” note. |
| **P5-4** | Before/after text mockups. | `docs/assets/` | none | CLI + Telegram mockups for verbose bubble and `/sessions`. |
| **P5-5** | Document new keys. | `docs/CONFIGURATION.md` | none | Keys in §6 appear in CONFIGURATION.md. |
| **P5-6** | Parity checklist: no unmarked Partial. | `docs/TELEGRAM_GATEWAY_PARITY.md` | none | Every new row is Implemented or explicit Deferred. |
| **P5-7** | CHANGELOG + pytest count. | `CHANGELOG.md`, advertised collection count | CI | Unreleased notes; count synced. |
| **P5-8** | Do not tag unless explicitly asked. | none | none | No git tag from this program. |

---

## 6. Config keys (intended)

Add only what a consumer in P1–P4 needs. Schema + pydantic + defaults +
SETTINGS in the same change as the first reader. `/verbose` and `/busy`
cycles stay session-only.

```yaml
ui:
  tool_progress: new          # already shipped — CLI
  busy_input_mode: queue      # P1-6 / P4-1: queue | steer | interrupt
  busy_ack_detail: false      # P4-3

gateway:
  telegram:
    token: ${JARN_TELEGRAM_BOT_TOKEN}    # already shipped
    allowed_user_ids: []                 # already shipped
    tool_progress: off                   # P2-6 overlay; unset/off = quiet
    tool_progress_cleanup: delete        # P2-5: delete | keep
    long_running_notifications: true     # P2-4
    busy_input_mode: steer               # P4-2 overlay; do not inherit CLI queue
```

`gateway.telegram.tool_progress` when **unset** must not inherit
`ui.tool_progress: new`. Telegram quiet default is a #40 contract.

---

## 7. File-level ownership

| Area | Primary files |
|---|---|
| This SSOT | `docs/specs/2026-08-15-hermes-parity-closeout.md` |
| Visual grammar (do not reopen) | `tui/palette.py`, `tui/grammar.py`, `tui/layout.py` |
| Toolbar | `src/jarn/tui/toolbar.py` |
| Paste / busy keys | `src/jarn/repl/keys.py`, `src/jarn/repl/app.py` |
| Resume picker | `src/jarn/repl/commands.py` |
| Recap | `src/jarn/controller/commands/diagnostics.py` |
| Command catalog | `src/jarn/commands/registry.py` |
| Diff widget | `src/jarn/tui/widgets/diff.py` |
| Background jobs | `src/jarn/agent/background.py` |
| Gateway worker | `src/jarn/gateway/worker.py` |
| Telegram outbox | `src/jarn/telegram/outbox.py`, `htmlutil.py` |
| Telegram bot | `src/jarn/telegram/bot.py` |
| YOLO card | `outbox.build_yolo_confirm_card`, `async_ops.set_permission_mode` |
| Config | `schema.py`, `pydantic_schema.py`, `defaults.py`, `settings.py` |
| Docs retarget (P0-2+) | display-standard spec, `ROADMAP.md`, `TELEGRAM_GATEWAY_PARITY.md`, `CONFIGURATION.md` |

---

## 8. PR sequence

1. **Docs contract** — P0-1 … P0-5 (this file first).
2. **CLI remaining** — P1-1 … P1-9.
3. **Telegram progress** — P2-1 … P2-8.
4. **Telegram commands** — P3-1 … P3-10.
5. **Busy UX** — P4-1 … P4-6.
6. **Evidence** — P5-1 … P5-8.

Do **not** mix a renderer rewrite (P2 progress bubble) with a command-layer
rewrite (P3 registry layers) in one review. P1 may land as more than one PR
(paste/toolbar vs `/diff`/`/busy`) but should not include Telegram live
edits.

---

## 9. Testing strategy

Match existing tests; extend them rather than starting a parallel suite.

| Area | Existing tests to extend |
|---|---|
| Outbox drop / cards / draft | `tests/test_telegram_outbox.py` |
| Layout + `GATEWAY_*` sets | `tests/test_layout.py` |
| REPL paste, help, turns | `tests/test_repl.py`, `tests/test_repl_paste.py` |
| Toolbar | `tests/test_phase3.py` (`render_toolbar` cases; no `test_toolbar.py`) |
| README command table | `tests/test_phase3.py` `readme_command_rows()` |
| Status recap | `tests/test_status_recap.py` |
| Diff widget | `tests/test_diff_render.py` |
| Background jobs | `tests/test_background.py` |
| Worker local slash / busy | `tests/test_gateway_worker.py` |
| Bot slash intercept | `tests/test_telegram_bot.py` |
| `NO_COLOR` / dumb | `tests/test_terminal_contract.py`, `tests/test_ux.py` |
| Terminal contract for new helpers | include new `layout` helpers in the existing CSI ban |

P2-8 and P3-10 are the Telegram pytest floors before P5 UAT.

---

## 10. Compatibility

- No breaking CLI flags. New keys default so today’s Telegram stays quiet and
  today’s CLI still queues busy Enter.
- Do not persist `/verbose` or `/busy` cycles unless `/config set`.
- Slash names stay. Add `/diff` `/busy`; alias `/resume` → `/sessions` in the
  REPL; do not rename `/cost`.
- Telegram HTML parse mode only (never MarkdownV2). Help chunks at 4096.
- Headless `-p` / `jarn exec` are out of this close-out except shared error
  rendering already shipped.
- Safety: no remote ALWAYS; YOLO card; untrusted clamp; `!` host-direct and
  red (`layout.host_shell`).

---

## 11. Non-goals (frozen)

Explicitly out of this close-out and not to be smuggled into P1–P5:

- Voice STT/TTS, Discord voice; still refuse voice notes (#54)
- Group chat / multi-user
- Discord, Slack, WhatsApp, or other Hermes platforms
- Personalities, kawaii, pets, `/skin` marketplace
- Second Ink / alt-screen TUI (stay native scrollback + prompt_toolkit)
- Remote ALWAYS
- Making `!` go through approvals (keep red host-direct)
- Web UI, open-core, native Windows
- Rewind slice 3–4 (in-place same-thread rewind, visual branch tree)

P0-5 echoes this list into ROADMAP / gateway parity. Changing a non-goal
needs a spec amendment, not a drive-by PR.

---

## 12. Success

A Hermes user can launch `jarn`, read the splash and toolbar, paste a stack
trace as one preview line, filter `/sessions`, and use `/diff` / `/busy`
without a second manual. In Telegram they get a quiet draft by default, a
single progress bubble after `/verbose`, a working heartbeat on long quiet
turns, and local `/status` `/help` `/model` `/mode` `/compact` `/undo`
`/sessions` `/resume <id>` `/title`. `/config set` tells them to use the
terminal. YOLO is still a card. `!` is still red.

When P5-6 has no unmarked Partial and P5-8 has not tagged, the close-out is
done.
