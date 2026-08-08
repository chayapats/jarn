# Telegram gateway — v1 parity checklist

Acceptance table for the shipped **VPS long-poll DM appliance** against map
[#26](https://github.com/chayapats/jarn/issues/26) and the binding plan in
[TELEGRAM_GATEWAY_PLAN.md](TELEGRAM_GATEWAY_PLAN.md) / [#44](https://github.com/chayapats/jarn/issues/44).

Statuses: **Implemented** (v1 done), **Partial** (in tree but short of the closed
decision), **Deferred** (explicitly out of v1). Ops deploy notes:
[TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md).

## Closed-decision rows

| Decision | Status | Module pointer |
|---|---|---|
| DM-only single-operator ([#34](https://github.com/chayapats/jarn/issues/34)) | Implemented | `src/jarn/telegram/auth.py`, `src/jarn/telegram/bot.py` — private chat + deny-by-default `from.id` on messages and callbacks |
| VPS long-poll only; laptop TUI not Telegram-commandable ([#53](https://github.com/chayapats/jarn/issues/53)) | Implemented | `src/jarn/telegram/bot.py`, `src/jarn/telegram/poller_lock.py`, `docs/TELEGRAM_GATEWAY.md` — one `getUpdates` consumer; TUI remains a separate local process |
| Worker-per-root + private NDJSON pipe ([#35](https://github.com/chayapats/jarn/issues/35) / [#60](https://github.com/chayapats/jarn/issues/60)) | Implemented | `src/jarn/gateway/daemon.py`, `worker.py`, `protocol.py`, `lease.py` — supervised per-root workers; handshake-versioned private frames; root `flock` |
| Park-and-resume approvals, no TTL ([#37](https://github.com/chayapats/jarn/issues/37)) | Implemented | `src/jarn/gateway/approvals.py` (+ worker/daemon resume path) — interrupt in `<root>/.jarn/state.sqlite` is SoT; gateway map is routing only; silence ≠ deny |
| `auto-edit` floor; no remote ALWAYS ([#39](https://github.com/chayapats/jarn/issues/39)) | Implemented | `src/jarn/telegram/outbox.py` (Once / Session / Deny; plan three-way; memory/skill Save/Decline); `src/jarn/gateway/approvals.py` (ALWAYS → SESSION) |
| Draft → finalize HTML; tool progress OFF ([#40](https://github.com/chayapats/jarn/issues/40)) | Implemented | `src/jarn/telegram/outbox.py` — `sendMessageDraft` → `sendMessage`; HTML; drop tool/subagent progress; restart draft after cards |
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
| Full 36-command chat surface | Deferred | Minimal chat commands (`/stop`, `/new`, `/repo`, approval callbacks); full REPL command parity not in v1. |

## Wave 5 note

This checklist is **T-QA-3**. Wave 5 hardening is complete: the turn
re-entrancy guard (**T-QA-1**) and scripted DM→park→resume e2e coverage
(**T-QA-2**) landed with the gateway in
[#88](https://github.com/chayapats/jarn/pull/88). The deferred/out-of-scope
rows above remain deliberate exclusions from v1.
