# jarn-cli

**J.A.R.N. — Just A Reliable Nerd.** A TUI-first coding agent harness built on
[DeepAgents](https://github.com/langchain-ai/deepagents), distributed here as a
**self-contained binary — no Python required**.

```bash
npm install -g jarn-cli
jarn            # or: jarn-cli
```

This package installs a small launcher exposed as both `jarn` and `jarn-cli`. The
actual program is a standalone binary shipped in a per-platform package
(`jarn-cli-<platform>-<arch>`) that npm installs automatically for your machine.

The v0.10 binary also includes the optional single-operator Telegram gateway. After
adding the global `gateway:` configuration and token, run:

```bash
export JARN_TELEGRAM_BOT_TOKEN='123456:replace-me'
jarn gateway
```

See the [gateway setup and systemd guide](https://github.com/chayapats/jarn/blob/main/docs/TELEGRAM_GATEWAY.md).

## Supported platforms

| OS | Architecture | Package |
|---|---|---|
| Linux | x64 | `jarn-cli-linux-x64` |
| Linux | arm64 | `jarn-cli-linux-arm64` |
| macOS | arm64 (Apple Silicon) | `jarn-cli-darwin-arm64` |

**Intel macs** and **native Windows** are not supported via npm — install with
`pip install jarn` (Intel mac), or run under **WSL** (Windows, uses the Linux
binary). On any unsupported host the `jarn` command prints these instructions.

> Installing with `--ignore-scripts` is fine (this package has no install
> scripts), but `--no-optional` will skip the binary and `jarn` will refuse to
> run with a message telling you to reinstall.

## Other ways to install

- **pip / uv:** `pip install jarn` (or `uv tool install jarn`) — same program from
  PyPI; requires Python 3.12+.
- This npm package and the PyPI package share the same version.

## Links

- Source, docs, and issues: <https://github.com/chayapats/jarn>
- Changelog: <https://github.com/chayapats/jarn/blob/main/CHANGELOG.md>
- License: Apache-2.0

The source line targets **v1.0.6 GA**. The protected release workflow promotes the
exact GitHub Release binaries, PyPI package, and npm packages in lockstep; consult
those registries for the currently published version.
