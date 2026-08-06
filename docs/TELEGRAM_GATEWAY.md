# Telegram gateway — ops (VPS long-poll)

v1 is a **VPS-only** long-poll DM appliance ([#53](https://github.com/chayapats/jarn/issues/53)). The laptop TUI is unchanged and is not Telegram-commandable. Full architecture lives in [TELEGRAM_GATEWAY_PLAN.md](TELEGRAM_GATEWAY_PLAN.md). v1 acceptance / deferred rows: [TELEGRAM_GATEWAY_PARITY.md](TELEGRAM_GATEWAY_PARITY.md).

This page covers deploy hardening for **T-OPS-1**: systemd, second-poller / 409 stand-down, and project `.jarn/.gitignore`.

## Process model (reminder)

- One transport process owns the bot token and `getUpdates` long-poll (`python -m jarn.telegram` today; `jarn gateway` lands in T-OPS-2).
- Per-root workers are supervised separately (private NDJSON pipe).
- **Never call Telegram `logOut`** — that invalidates the token for every client. Conflict handling stands down; it does not log out.

## systemd unit (example)

Install as a dedicated user (example: `jarn`). Pin the working directory to that user's home or a deploy checkout. Put the bot token in the unit environment (or a `EnvironmentFile=` with `0600` perms) — do not commit tokens.

```ini
# /etc/systemd/system/jarn-telegram.service
[Unit]
Description=J.A.R.N. Telegram gateway (long-poll)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=jarn
Group=jarn
WorkingDirectory=/home/jarn

# Prefer a venv or uv-managed interpreter with the telegram extra installed:
#   uv sync --python 3.12 --extra telegram
# ExecStart forms (pick one):
#   ExecStart=/home/jarn/.venv/bin/python -m jarn.telegram
#   ExecStart=/usr/local/bin/jarn gateway   # T-OPS-2 CLI, when available
ExecStart=/home/jarn/.venv/bin/python -m jarn.telegram

Restart=on-failure
RestartSec=5
# Stand-down exits must not bounce into another conflict loop (#53):
#   75 = EXIT_CONFLICT  (Telegram 409 — another getUpdates client)
#   76 = EXIT_LOCK_HELD (host flock already held)
RestartPreventExitStatus=75 76

# Token + allowlist (deny-by-default). Prefer EnvironmentFile= for secrets.
Environment=JARN_TELEGRAM_BOT_TOKEN=REPLACE_ME
Environment=JARN_TELEGRAM_ALLOWED_USER_IDS=123456789
# Optional: Environment=JARN_HOME=/home/jarn/.jarn

# Soft hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jarn-telegram.service
sudo systemctl status jarn-telegram.service
```

Notes:

- **VPS-only for v1** — do not run a second long-poller on a laptop against the same bot token.
- Config may also supply `gateway.telegram.token` / `allowed_user_ids` in `~/.jarn/config.yaml` (env wins when set by `__main__` today).
- Full CLI packaging is **T-OPS-2**; until then `python -m jarn.telegram` is the entry.

## Second poller / 409 behavior

Telegram allows only one active `getUpdates` consumer per bot token. Jarn enforces this in two layers (`src/jarn/telegram/poller_lock.py` + `bot.py`):

| Layer | What | Exit |
|---|---|---|
| Host flock | `~/.jarn/gateway/telegram.poll.lock` — non-blocking exclusive `flock`. Same-host second start fails immediately. | `EXIT_LOCK_HELD` (**76**) |
| Telegram 409 | First `TelegramConflictError` from long-poll → one chat notice → process exit. **Never retry 409.** | `EXIT_CONFLICT` (**75**) |

Invariants:

1. Acquire the host flock **before** opening the long-poll loop.
2. On the **first** 409: send **one** chat message (notify chat or first allowlisted user), then return `EXIT_CONFLICT`. Do **not** sleep-and-retry the conflict.
3. Transient non-409 errors may pause briefly and continue; they are not treated as conflict.
4. **Never call `logOut`** (not on conflict, not on shutdown, not on webhook report).
5. Wire systemd `RestartPreventExitStatus=75 76` so stand-down does not restart-loop.

Cross-host exclusion is impossible with flock alone — the 409 path is the remote fence.

## Project `.jarn/.gitignore`

When the gateway binds a project root (personal root ensure, session `/repo`, or worker spawn), jarn writes an idempotent `<root>/.jarn/.gitignore` so DM transcripts, SQLite state, logs, and lock siblings are not pushed. Helper: `jarn.config.paths.ensure_project_gitignore`.

Committed project config / skills / wiki under `.jarn/` stay trackable; only runtime paths are ignored.
