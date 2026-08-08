# Jarn Telegram Gateway — Spec + Implementation Plan (#44)

Destination of map [#26](https://github.com/chayapats/jarn/issues/26). Binding decisions cited inline. **v1 = VPS long-poll DM appliance**; laptop TUI unchanged and not Telegram-commandable ([#53](https://github.com/chayapats/jarn/issues/53)).

> **Status:** shipped in **v0.10.0** on 2026-08-08. W0–W5 are complete; this file is
> retained as the implementation decision record. For current operator instructions,
> use [TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md).

## Architecture (shipped)

### Process topology ([#35](https://github.com/chayapats/jarn/issues/35) C′, [#53](https://github.com/chayapats/jarn/issues/53) C)
- **Transport daemon** (systemd on VPS) owns the bot token + long-poll (`aiogram`).
- **One long-lived worker process per project root** over a **private** local pipe ([#60](https://github.com/chayapats/jarn/issues/60)).
- Evict when: idle ∧ no live bg job ∧ no turn in flight. Parked approvals do **not** pin workers ([#37](https://github.com/chayapats/jarn/issues/37)).
- Mutual exclusion **per root** via `flock`; first holder wins; gateway refuses in chat ([#52](https://github.com/chayapats/jarn/issues/52)).
- Second poller: host flock, stand down on first 409 (one chat msg + distinct exit; never retry 409). Never call `logOut`.

### Bridge location ([#36](https://github.com/chayapats/jarn/issues/36) A)
- In-tree `src/jarn/telegram/` behind `telegram` extra gating **only** `aiogram`.
- Code ships to every pip/npm/binary user; extra gates **dependencies, never code**.
- Pipe is PRIVATE; no public embedding API; stream-json is not a contract.
- Peer to `repl/` / `headless.py`; may use privates. Do **not** become a third consumer of `_run_headless` — settle/export proper APIs instead.
- Export `Approver` at `jarn.agent` + test. Do not add `py.typed`.
- `jarn doctor` must report: `gateway:` configured but `telegram` extra not installed.
- Delivery: Python installs use the optional `telegram` extra; the npm/frozen binary
  bundles aiogram. Both expose `jarn gateway`; `python -m jarn.telegram` remains a
  compatibility entry point.

### Session model ([#38](https://github.com/chayapats/jarn/issues/38), [#51](https://github.com/chayapats/jarn/issues/51))
- `(chat_id, root) → thread_id`. Default root = `~/.jarn/personal` (git init). `/repo` only to `gateway:` allowlist. Hard-refuse `$HOME` / global-config collision.
- Busy: queue + notice + `/stop`. `/new` mints thread_id (#47: undo/redo refuse foreign-tagged tops).
- Auth: DM-only; deny-by-default; principal = `from.id` on every update including callbacks ([#34](https://github.com/chayapats/jarn/issues/34)).

### Approvals ([#37](https://github.com/chayapats/jarn/issues/37), [#39](https://github.com/chayapats/jarn/issues/39))
- Durable park-and-resume; interrupt in `<root>/.jarn/state.sqlite` is SoT; the
  gateway store maps tokens to root/thread/chat and retains redacted card metadata.
- No TTL. Restart re-cards allowlisted chats. Callbacks are chat-bound and resume
  the stored root/thread. Tool-only cards use redacted args.
- Floor `auto-edit`; Once/Session/Deny only — no remote ALWAYS. Plan-mode three-way. Memory Save/Decline. No edit-before-apply on chat.

### Output ([#40](https://github.com/chayapats/jarn/issues/40))
- `sendMessageDraft` → finalize `sendMessage`; HTML; tool progress OFF; drop subagent inner stream; restart draft after any real message.

### Redaction ([#36](https://github.com/chayapats/jarn/issues/36))
- **Worker** fail-closed redacts what may leave (**before** serialization; must cover denied path — fix TOOL_START-before-interrupt order).
- **Daemon** truncates/coalesces for Telegram display.

### Pipe ([#60](https://github.com/chayapats/jarn/issues/60))
- Bidirectional NDJSON; distinct private schema; version on handshake only.
- Inbound v1: `handshake` | `turn` (including delivery `chat_id`) |
  `approval_verdict` | `cancel` | `steer` | `shutdown`.
- Worker death: fail-loud, no auto-replay. Periodic `status` heartbeats. OS-pipe backpressure.

### Media ([#54](https://github.com/chayapats/jarn/issues/54))
- Core multimodal API + gates. Images ≤5 MB. Docs stage outside root → `read_file` → delete. Voice/unsupported = refusal card. Delete `execution.multimodal`. Share inline_images/T-3-7 via [#41](https://github.com/chayapats/jarn/issues/41) turn runner.

### Skills/memory ([#43](https://github.com/chayapats/jarn/issues/43))
- Skills suggest-then-approve; memory gate unconditional; fix recursive `SKILL.md`; write to `<active_root>/.jarn/skills`.

### Commands ([#59](https://github.com/chayapats/jarn/issues/59))
- Async `controller.undo/redo/abort`; yolo via controller-owned confirm; REPL migrates to same path.

### Scheduler ([#42](https://github.com/chayapats/jarn/issues/42))
- In-gateway; park+push approvals; catch-up once; agent self-schedule tool; jobs default personal root.

---

## Config schema (`gateway:`) — implemented

`_GLOBAL_ONLY_KEYS` includes `gateway`. The project tier strips `gateway:` and warns
(trusted and untrusted are identical). Tokens resolve through `config/secrets.py` via
`keychain:` / `${ENV}` / `file:`; `strict_secrets` does not catch inline bot tokens
today, so prefer a non-inline reference.

Implemented shape:

```yaml
gateway:
  enabled: true
  telegram:
    token: ${JARN_TELEGRAM_BOT_TOKEN}   # or keychain:/file:
    allowed_user_ids: [123456789]      # entire remaining auth boundary under #34
  repos:                                # /repo allowlist
    - path: /srv/repos/myapp
      name: myapp
  # optional later: draft coalescing knobs
```

---

## Task breakdown (one Sub Agent session each)

### Wave 0 — Spec artifact (this ticket)
| ID | Task | Owns | Done when |
|---|---|---|---|
| T-SPEC-1 | Write/post this plan + architecture into #44; close #44 | tracker | #44 closed with plan comment |

### Wave 1 — Foundation (PARALLEL — no overlapping files)
| ID | Task | Owns | Done when |
|---|---|---|---|
| T-CORE-1 | Export `Approver` from `jarn.agent` + `hasattr` test | `agent/__init__.py`, new test | test passes |
| T-CORE-2 | Fix recursive skill discovery `skills/<name>/SKILL.md` | `extensibility/` | tests for nested layout |
| T-CORE-3 | Delete dead `execution.multimodal`; add `_GLOBAL_ONLY_KEYS`; stub `gateway:` schema (strict); strip+warn on project tier; personal-root helper `ensure_personal_root()` | `config/**` | unit tests; doctor can see key |
| T-PIPE-1 | Private NDJSON protocol types + serde + handshake version | **new** `src/jarn/gateway/` only | tests round-trip frames |
| T-PIPE-2 | Per-root `flock` lease helper (first holder wins) | **new** `src/jarn/gateway/lease.py` (+tests) | contended acquire fails cleanly |
| T-SEC-1 | Fail-closed redaction **before** TOOL_START / denied path cannot leak args | `agent/stream_handlers.py` (+tests) | measured leak cases fail closed |
| T-TG-1 | `src/jarn/telegram/` package skeleton + `telegram` extra (`aiogram` only) + doctor message for configured-but-missing extra | `telegram/**`, `pyproject.toml`, `doctor/**` | import guard; doctor string |

### Wave 2 — Shared agent loop (SERIALIZE hot core; 2–3 agents max)
| ID | Task | Owns | Depends |
|---|---|---|---|
| T-CTRL-1 | Async `controller.undo/redo/abort` (+ settle); refuse silent yolo escalate; migrate REPL wrappers | `controller/**`, `repl/commands.py`, `repl/keys.py` | W1 |
| T-TURN-1 | Extract shared turn runner from `repl/turn.py` (retry/fallback/T-3-7/inline_images) for REPL/headless/bridge | `repl/turn.py`, new `agent/turn_runner.py` or similar, `headless.py` | T-CTRL-1 preferred |
| T-MEDIA-1 | Core multimodal ingest API (bytes/mime or path+modality) + 5 MB image gate + allowlist | `agent/session.py`, `agent/files.py` | T-TURN-1 can parallel if careful |
| T-MEDIA-2 | Document staging outside root + path injection + cleanup; unsupported/voice refusal helper | new helper under `agent/` or `telegram/media.py` | T-MEDIA-1 |
| T-APPR-1 | Durable pending-approval gateway map + park approver + resume path (`Command(resume=…)`) | `gateway/approvals.py`, session resume API | T-CTRL-1, T-PIPE-1 |
| T-SKILL-1 | `suggest_skill` tool + approve→write `<root>/.jarn/skills` | `extensibility/`, `builtin_tools`, interrupts | T-CORE-2 |

### Wave 3 — Daemon + worker (PARALLEL by package)
| ID | Task | Owns | Depends |
|---|---|---|---|
| T-WKR-1 | Worker main loop: handshake, status heartbeats, turn/cancel/steer/shutdown, emit events | `gateway/worker.py` | T-PIPE-1, T-TURN-1, T-APPR-1 |
| T-WKR-2 | Worker-side redaction at serialize boundary; eviction status fields | `gateway/worker.py` | T-SEC-1, T-WKR-1 |
| T-DMN-1 | Daemon supervisor: spawn/reap per-root workers, pipe I/O, OS backpressure, fail-loud on death | `gateway/daemon.py` | T-PIPE-1, T-PIPE-2 |
| T-DMN-2 | Session routing `(chat_id,root)→thread_id`, queue-when-busy, `/stop`, `/new`, `/repo` | `gateway/sessions.py` | T-DMN-1, T-CORE-3 |
| T-TG-2 | aiogram long-poll app: DM-only auth, principal on callbacks, backlog report-not-execute | `telegram/bot.py` | T-TG-1, T-DMN-1 |
| T-TG-3 | Output: draft→finalize HTML; approval cards; media refusal cards; restart draft after cards | `telegram/outbox.py` | T-TG-2, T-APPR-1 |
| T-TG-4 | Inbound media download→stage/refuse per #54; wire into `turn` frames | `telegram/inbound_media.py` | T-MEDIA-2, T-TG-2 |

### Wave 4 — Scheduler + ops
| ID | Task | Owns | Depends |
|---|---|---|---|
| T-SCHED-1 | In-gateway job store + catch-up-once + personal default root | `gateway/scheduler.py` | T-DMN-2 |
| T-SCHED-2 | Agent self-schedule tool + park+push for scheduled approvals | tools + T-APPR-1 | T-SCHED-1 |
| T-OPS-1 | systemd unit docs; second-poller flock+409 behavior; `<root>/.jarn/.gitignore` writer — see [TELEGRAM_GATEWAY.md](TELEGRAM_GATEWAY.md) | `docs/TELEGRAM_GATEWAY.md`, `config/paths.py`, gateway bind sites | T-TG-2 |
| T-OPS-2 | CLI `jarn gateway` (or equivalent) entry; integration smoke test (mocked aiogram) | `cli.py`, tests | T-TG-3 |

### Wave 5 — Harden + close gaps
| ID | Task | Owns | Depends |
|---|---|---|---|
| T-QA-1 | Turn re-entrancy guard per thread_id (no silent interleave) | controller/session | T-TURN-1 |
| T-QA-2 | End-to-end scripted test: DM→turn→approval park→verdict→resume | tests | W3 |
| T-QA-3 | Parity table checklist vs #26 deferred/out-of-scope; update #26 if needed | tracker | W4 — **done:** [TELEGRAM_GATEWAY_PARITY.md](TELEGRAM_GATEWAY_PARITY.md) (closed-decision rows Implemented; voice/group/embed/ALWAYS/laptop/stream-json Deferred) |

Status: W0–W5 are complete and merged via [#88](https://github.com/chayapats/jarn/pull/88),
including the **T-QA-1** re-entrancy guard and **T-QA-2** scripted e2e path.
**T-QA-3** is the parity document above; its deferred rows remain out of v1.

---

## Parallelism rules
1. Wave 1 agents must not edit outside their **Owns** column.
2. Hot core (`controller/`, `repl/turn.py`, `agent/session.py`) = at most one writer at a time (Wave 2).
3. Prefer new packages `gateway/` and `telegram/` for fan-out.
4. Each task: implement + tests with `uv run --python 3.12 --extra dev pytest <paths>`; no drive-by refactors.
5. Do not implement voice STT/TTS (deferred).

## Out of v1 (do not build)
Voice STT/TTS; group chat; public embedding API; remote ALWAYS; laptop Telegram takeover; freezing stream-json; full 36-command chat surface.

Tracked with status pointers in [TELEGRAM_GATEWAY_PARITY.md](TELEGRAM_GATEWAY_PARITY.md).
