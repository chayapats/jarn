# Telegram gateway — setup and VPS operations

The optional Telegram gateway shipped in **v0.10.0**. It is a single-operator,
DM-only, VPS long-poll appliance: one transport process owns the bot token and
supervises one isolated worker process per active project root. The laptop TUI is
unchanged and cannot be controlled through Telegram.

For design decisions and acceptance scope, see
[TELEGRAM_GATEWAY_PLAN.md](TELEGRAM_GATEWAY_PLAN.md) and
[TELEGRAM_GATEWAY_PARITY.md](TELEGRAM_GATEWAY_PARITY.md).
v1.1 display-parity (quiet default, opt-in `/verbose` bubble, local command
layers, mutating refuse, second-DM steer) is **Implemented** on this branch.
v1 still refuses voice notes, stays DM-only, and never grants remote ALWAYS.

Streaming drafts are coalesced before they hit Telegram (`sendMessageDraft` at
most about twice a second, or sooner if a few hundred unsent characters pile
up) so a long reply still appears progressively instead of stalling on rate
limits. An approval, yolo, or undo card first persists any live prose as a real
message, then posts the card; tapping a button deletes that card from the
chat (if delete fails, the buttons are stripped). Quiet default remains tool
progress off.

## Install and configure

The npm/standalone distribution already includes Telegram support. A Python install
must include the optional extra:

```bash
npm install -g jarn-cli
# or
pip install 'jarn[telegram]'
# or, from a checkout
uv sync --extra dev --extra telegram
```

Run the guided setup; do not paste the token into a command or edit YAML:

```bash
jarn gateway setup
```

The wizard performs the complete safe path:

1. Prompts for the BotFather token with terminal echo disabled.
2. Calls Telegram `getMe` to verify the exact bot and refuses an active webhook.
3. Shows the bot link and asks you to send `/start`; the next private message is used
   to discover your numeric user ID. Multiple senders require an explicit choice.
4. Shows a secret-free summary and waits for confirmation before writing anything.
5. Stores the token in the OS keychain, with an owner-only J.A.R.N. file fallback;
   the YAML receives only a secret reference.
6. Locks, backs up, validates, and atomically updates the global config. A failed
   commit rolls back the newly staged credential.
7. On Linux with a user systemd manager, offers to install and start an owner-scoped
   service. It never puts the token in the unit or an environment file.

Inspect or control that service without opening config files:

```bash
jarn gateway status
jarn gateway install-service
jarn gateway start
jarn gateway stop
jarn gateway restart
```

If the optional user service cannot start, the verified bot config and
credential remain usable with `jarn gateway`. Correct the reported systemd
cause, then run `jarn gateway install-service`; you do not need to re-enter the
token or send `/start` again.

For SSH/automation, send the token over stdin rather than argv and supply a known
numeric ID explicitly:

```bash
printf '%s\n' "$JARN_TELEGRAM_BOT_TOKEN" | \
  jarn gateway setup --token-stdin --allowed-user 123456789 --yes
```

### Advanced manual configuration

The wizard is the supported default. If an operator deliberately manages secrets
and services externally, the equivalent global-only configuration is:

```yaml
gateway:
  enabled: true
  telegram:
    token: ${JARN_TELEGRAM_BOT_TOKEN}
    allowed_user_ids: [123456789]
  repos:
    - path: /srv/repos/myapp
      name: myapp
```

Then export the referenced secret and start the daemon:

```bash
export JARN_TELEGRAM_BOT_TOKEN='123456:replace-me'
jarn gateway
```

`python -m jarn.telegram` remains a backwards-compatible entry point. Prefer
`jarn gateway` in scripts and service units.

Startup refuses to continue unless `gateway.enabled` is true, the bot token resolves,
and the allowlist is non-empty. Environment variables take precedence when non-empty:

| Variable | Purpose |
|---|---|
| `JARN_TELEGRAM_BOT_TOKEN` | Bot token; overrides `gateway.telegram.token` |
| `JARN_TELEGRAM_ALLOWED_USER_IDS` | Comma-separated numeric IDs; overrides the config list |
| `JARN_TELEGRAM_FAKE_BACKEND=1` | In-memory transport dry-run; no project workers |

`jarn gateway --fake-backend` is the CLI equivalent of the last variable. It still
connects to Telegram, but uses an in-memory backend instead of spawning workers.

The default working root is `~/.jarn/personal`, created and initialized as a Git
repository on demand. `/repo` can switch only to roots in `gateway.repos`.

## Process and security model

- The transport process owns Telegram `getUpdates`; per-root workers communicate over
  a private, versioned NDJSON pipe.
- DMs and callback queries are authenticated by the sender's numeric user id. Group
  chats and users outside the allowlist are rejected.
- Approvals are durable. The worker persists the redacted card, original root,
  thread, and Telegram chat before emitting it. A restart re-displays cards only
  for chats that remain allowlisted, and callbacks resume the stored root/thread
  rather than whichever repository happens to be active after boot. Silence is
  not a denial. Remote approvals never grant `ALWAYS`.
- Photos and documents are gated and staged for the active worker. Voice messages are
  explicitly refused; STT/TTS is not included in v1.
- Updates already waiting when the process starts are counted, reported, and discarded
  rather than executed as agent instructions.
- Each root has an exclusive gateway lease, preventing another gateway process or
  worker from controlling the same root concurrently.
- **Never call Telegram `logOut`** for conflict recovery; that invalidates the token for
  every client. The gateway stands down instead.

Keep `~/.jarn` private (`0700`); Jarn enforces this mode where POSIX permissions are
available. On a shared host, also audit ACLs and run the service under a dedicated user.
See [../SECURITY.md](../SECURITY.md#telegram-gateway-vps) for the complete boundary.

## Advanced systemd system unit

The setup wizard normally creates a token-free **user** service automatically. For
central multi-user VPS administration, install as a dedicated system user (example:
`jarn`). Put the token in an
`EnvironmentFile=` with `0600` permissions instead of committing it or embedding it
in the unit.

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
ExecStart=/usr/local/bin/jarn gateway

EnvironmentFile=/home/jarn/.config/jarn/telegram.env
# Optional: Environment=JARN_HOME=/home/jarn/.jarn

Restart=on-failure
RestartSec=5
# 75 = Telegram 409 conflict; 76 = same-host poller lock already held;
# 77 = invalid or unauthorized bot token. All require operator action.
RestartPreventExitStatus=75 76 77

# Let the main process terminate workers cleanly, then stop the whole cgroup.
KillMode=mixed
TimeoutStopSec=30s
UMask=0077

# Soft hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

If installed in a virtual environment, use its executable instead, for example
`ExecStart=/home/jarn/app/.venv/bin/jarn gateway`.

Create the environment file and enable the service:

```bash
sudo install -d -m 0700 -o jarn -g jarn /home/jarn/.config/jarn
sudoedit /home/jarn/.config/jarn/telegram.env
sudo chmod 0600 /home/jarn/.config/jarn/telegram.env
sudo chown jarn:jarn /home/jarn/.config/jarn/telegram.env

sudo systemctl daemon-reload
sudo systemctl enable --now jarn-telegram.service
sudo systemctl status jarn-telegram.service
journalctl -u jarn-telegram.service -f
```

The environment file uses shell-style assignments without `export`:

```bash
JARN_TELEGRAM_BOT_TOKEN=123456:replace-me
JARN_TELEGRAM_ALLOWED_USER_IDS=123456789
```

## Terminal stand-down conditions

Telegram allows only one active `getUpdates` consumer per bot token. Jarn enforces
this in two layers (`src/jarn/telegram/poller_lock.py` and `bot.py`):

| Layer | Behaviour | Exit |
|---|---|---|
| Host flock | `~/.jarn/gateway/telegram.poll.lock` is acquired non-blocking. A second process on the same host exits immediately. | `EXIT_LOCK_HELD` (**76**) |
| Telegram 409 | The first conflict produces one operator notice and exits. It is never retried. | `EXIT_CONFLICT` (**75**) |
| Invalid/unauthorized token | Local token validation or Telegram HTTP 401 stops startup/polling immediately; permanent authentication errors are never retried as network failures. | `EXIT_UNAUTHORIZED` (**77**) |

Cross-host exclusion is impossible with `flock` alone; the Telegram 409 response is
the remote fence. `RestartPreventExitStatus=75 76 77` prevents systemd from
restarting any operator-action condition into a tight failure loop.

## Project `.jarn/.gitignore`

When the gateway binds a project root, Jarn idempotently writes
`<root>/.jarn/.gitignore`. This keeps DM transcripts, SQLite/checkpoint state, logs,
and lock siblings out of Git while leaving committed project config, skills, and wiki
content trackable. The shared helper is
`jarn.config.paths.ensure_project_gitignore`.

---

**Related docs:** [Configuration](CONFIGURATION.md#telegram-gateway-gateway--global-only) ·
[Architecture](ARCHITECTURE.md) · [Security](../SECURITY.md#telegram-gateway-vps) ·
[← docs index](README.md)
