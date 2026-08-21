# Telegram gateway — v1 parity checklist

Acceptance table for the shipped **VPS long-poll DM appliance** against map
[#26](https://github.com/chayapats/jarn/issues/26) and the binding plan in
[TELEGRAM_GATEWAY_PLAN.md](TELEGRAM_GATEWAY_PLAN.md) / [#44](https://github.com/chayapats/jarn/issues/44).

This scope shipped in **v0.10.0** on 2026-08-08. The table remains the acceptance
record; operators should use [TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md).

Statuses: **Implemented** (in this branch), **Deferred** (explicitly out of
scope). Do not leave an unmarked **Partial**. Ops deploy notes:
[TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md). Remaining CLI + Telegram display
SSOT:
[2026-08-15-hermes-parity-closeout.md](specs/2026-08-15-hermes-parity-closeout.md).

v1.1 rows below are Implemented on `feat/hermes-parity-closeout`. They are
**not** a GitHub Release.

## Closed-decision rows

| Decision | Status | Module pointer |
|---|---|---|
| DM-only single-operator ([#34](https://github.com/chayapats/jarn/issues/34)) | Implemented | `src/jarn/telegram/auth.py`, `src/jarn/telegram/bot.py` — private chat + deny-by-default `from.id` on messages and callbacks |
| VPS long-poll only; laptop TUI not Telegram-commandable ([#53](https://github.com/chayapats/jarn/issues/53)) | Implemented | `src/jarn/telegram/bot.py`, `src/jarn/telegram/poller_lock.py`, `docs/TELEGRAM_GATEWAY.md` — one `getUpdates` consumer; TUI remains a separate local process |
| Worker-per-root + private NDJSON pipe ([#35](https://github.com/chayapats/jarn/issues/35) / [#60](https://github.com/chayapats/jarn/issues/60)) | Implemented | `src/jarn/gateway/daemon.py`, `worker.py`, `protocol.py`, `lease.py` — supervised per-root workers; handshake-versioned private frames; root `flock` |
| Park-and-resume approvals, no TTL ([#37](https://github.com/chayapats/jarn/issues/37)) | Implemented | `src/jarn/gateway/approvals.py` (+ worker/daemon resume path) — interrupt in `<root>/.jarn/state.sqlite` is SoT; gateway map is routing only; silence ≠ deny |
| `auto-edit` floor; no remote ALWAYS ([#39](https://github.com/chayapats/jarn/issues/39)) | Implemented | `src/jarn/telegram/outbox.py` (Once / Session / Deny; plan three-way; memory/skill Save/Decline); `src/jarn/gateway/approvals.py` (ALWAYS → SESSION) |
| Draft → finalize HTML; tool progress OFF ([#40](https://github.com/chayapats/jarn/issues/40)) | Implemented | `src/jarn/telegram/outbox.py` — coalesced `sendMessageDraft` → `sendMessage`; HTML; drop tool/subagent progress; cards persist live prose then restart the draft; tapped cards are deleted |
| Media: photos/docs; voice refuse ([#54](https://github.com/chayapats/jarn/issues/54)) | Implemented | `src/jarn/telegram/inbound_media.py`, `src/jarn/agent/media_ingest.py` — image/doc ingest + gates; voice/unsupported → refusal card (caption may proceed) |
| Skills suggest-then-approve; memory gate ([#43](https://github.com/chayapats/jarn/issues/43)) | Implemented | `src/jarn/agent/builtin_tools.py` (`suggest_skill` / `suggest_memory`), `src/jarn/agent/interrupts.py`, cards in `src/jarn/telegram/outbox.py` — no autonomous skill/memory writes |
| Scheduler in-gateway; catch-up once ([#42](https://github.com/chayapats/jarn/issues/42)) | Implemented | `src/jarn/gateway/scheduler.py`, `schedule_task` in `src/jarn/agent/builtin_tools.py` — job store + catch-up-once tick; scheduled turns still park+push approvals |

## Explicitly out of v1 / deferred

| Item | Status | Notes |
|---|---|---|
| Voice STT / TTS | Deferred | Banked under #26 / withdrawn in [#36](https://github.com/chayapats/jarn/issues/36). v1 **refuses** voice notes (#54); does not transcribe or speak. |
| Group chat | Deferred | [#34](https://github.com/chayapats/jarn/issues/34) chose single-operator DM; multi-user/group Hermes rows out of scope. |
| Public embedding API | Deferred | Pipe is private ([#36](https://github.com/chayapats/jarn/issues/36) / [#60](https://github.com/chayapats/jarn/issues/60)); no supported embed surface. |
| Remote ALWAYS | Deferred | Forbidden on the chat path ([#39](https://github.com/chayapats/jarn/issues/39)); cards omit Always; verdict path downgrades. |
| Laptop Telegram takeover | Deferred | v1 is VPS long-poll only ([#53](https://github.com/chayapats/jarn/issues/53)); laptop TUI is not commandable over Telegram. |
| Freezing stream-json | Deferred | stream-json / public NDJSON is **not** a contract; gateway↔worker schema may change. |
| Full 36-command chat surface | Deferred | v1.1 adds a **local subset** (status/help/model/mode/compact/undo/sessions/resume/title/skill) plus mutating refuse. The rest of the REPL catalog stays off Telegram. |

## v1.1 display-parity

Readonly Telegram pages already use the shared layout HTML dialect (waves A–K,
[#92](https://github.com/chayapats/jarn/pull/92)). Live-turn and command muscle
memory is this table. Tracked in
[2026-08-15-hermes-parity-closeout.md](specs/2026-08-15-hermes-parity-closeout.md).
UAT: [UAT-HERMES-PARITY.md](assets/UAT-HERMES-PARITY.md) (live bot rows **Not run**).

| Item | Status | Notes |
|---|---|---|
| Quiet default (draft → finalize; tools off) | Implemented | [#40](https://github.com/chayapats/jarn/issues/40) stays. Default turn is prose only. Overlay `gateway.telegram.tool_progress` defaults `off` and does not inherit CLI `new`. |
| Opt-in `/verbose` progress bubble | Implemented | One edited HTML bubble; disappears when the answer lands (`tool_progress_cleanup: delete`). [#94](https://github.com/chayapats/jarn/pull/94). |
| Local command layers | Implemented | Local set: `/status` `/help` `/model` `/mode` `/compact` `/undo` `/sessions` `/resume <id>` `/title` (+ `/skill` seed). Not the full 36-command REPL surface (that row stays Deferred above). [#95](https://github.com/chayapats/jarn/pull/95). |
| Mutating refuse | Implemented | `/config set` and other `GATEWAY_MUTATING_COMMANDS` names refused with a terminal / `jarn` CLI hint; never `submit_turn`. [#95](https://github.com/chayapats/jarn/pull/95). |
| Second-DM steer | Implemented | Second text while a turn is in flight steers by default (`gateway.telegram.busy_input_mode: steer`; does not inherit CLI queue) or queues; one short `Working…` ack; never a second `_run_turn`. [#98](https://github.com/chayapats/jarn/pull/98). |
| `/background` slash wrapper | Deferred | P4-4 optional. `/ps` + background tools are sufficient ([#98](https://github.com/chayapats/jarn/pull/98)). |
| Ctrl+S input stash | Deferred | P4-5 optional; not required to close the program. CLI-only; N/A to this gateway. |

## Frozen non-goals (close-out)

Echo of the close-out spec §11. These stay **Deferred**. Changing one needs a
spec amendment, not a drive-by PR. This checklist does **not** promise:

- Voice STT/TTS; still refuse voice notes (#54) — also Deferred in v1 above
- Group chat / multi-user — also Deferred in v1 above
- Discord, Slack, WhatsApp, or other chat platforms
- Personalities, kawaii, pets, `/skin` marketplace
- Second Ink / alt-screen TUI (CLI stays native scrollback; N/A to this gateway)
- Remote ALWAYS — also Deferred in v1 above
- Making host-direct `!` go through approvals (`!` stays red on the CLI)
- Web UI, open-core, native Windows
- Rewind slices 3–4 (in-place same-thread rewind, visual branch tree)

## Wave 5 note

This checklist is **T-QA-3**. Wave 5 hardening is complete: the turn
re-entrancy guard (**T-QA-1**) and scripted DM→park→resume e2e coverage
(**T-QA-2**) landed with the gateway in
[#88](https://github.com/chayapats/jarn/pull/88). The deferred/out-of-scope
rows above remain deliberate exclusions from v1. v1.1 display-parity does not
reopen them. P4 keeps T-QA-1: one in-flight turn task per worker.
