# Supported platforms

This page is the installation contract for the current release candidate. The
installer checks these requirements before changing an existing installation. An
unsupported host fails early and keeps the current command and user data intact.

## Tier 1

| Platform | CPU | Runtime contract | Release-gated installer path |
|---|---|---|---|
| Ubuntu 20.04, 22.04, 24.04 | x86-64 | glibc 2.31 or newer | Verified Linux x86-64 release binary |
| Ubuntu 20.04, 22.04, 24.04 | ARM64 | glibc 2.31 or newer | Verified Linux ARM64 release binary |
| Debian 11, 12 | x86-64 | glibc 2.31 or newer | Verified Linux x86-64 release binary |
| Debian 11, 12 | ARM64 | glibc 2.31 or newer | Verified Linux ARM64 release binary |
| macOS 15 and 26 | Apple Silicon | Native ARM64 process | Verified macOS ARM64 release binary |
| macOS 15 and 26 | Intel | Native x86-64 process; managed Python 3.12 | Isolated managed-Python fallback using the candidate wheel |

Each Linux row is tested in that distribution's container on a native x86-64 or
ARM64 GitHub-hosted runner. Each macOS row runs on the corresponding native
Apple Silicon or Intel hosted runner. Every leg must pass clean install, command
startup, upgrade over an older command, rollback and roll-forward, uninstall,
preserved-data checks, and reinstall before a draft release can be promoted.

## Compatibility tier (not a release gate)

These environments are accepted by the installer and are expected to follow the
same underlying platform contract, but they do not run the complete lifecycle on
every release:

- Windows 10/11 with WSL2 running one of the Ubuntu versions above. The Linux
  userland and architecture are covered, but the actual Windows/WSL kernel,
  filesystem mounts, shell launch, and interop path are not exercised by the
  release workflow. WSL2 is therefore not Tier 1.
- macOS 13 and 14 on Apple Silicon or Intel. The installer accepts macOS 13 or
  newer, but releases outside the currently gated macOS 15/26 hosted images are
  compatibility-tier until a maintained lifecycle runner is added.

A compatibility-tier failure is investigated, but it does not by itself block a
release. Do not interpret installer acceptance as the stronger Tier-1 claim.

The standard install is user-space only. It does not require `sudo`, Node.js, a
system Python, or a preinstalled `uv`. It needs `sh`, `curl`, TLS access to GitHub
and the selected provider, a writable home/install directory, and at least 512 MiB
free during installation.

## Explicitly unsupported

- Native Windows and PowerShell installation. Install WSL2 Ubuntu, open its shell,
  and run the Linux installer there.
- Alpine Linux and other musl-based systems.
- Linux distributions outside the Ubuntu/Debian versions above, even when their
  libc happens to be compatible. An unverified fallback is not treated as support.
- glibc older than 2.31.
- macOS 12 or older.

## Terminal and shell support

Bash, zsh, and other POSIX-compatible login shells are supported. The installer
places the selected command under `~/.local/bin` by default and updates an
appropriate profile atomically. It inventories other `jarn` commands, aliases,
functions, and shell command caches; it does not delete or overwrite an unrelated
installation without explicit confirmation.

The interactive UI targets UTF-8 terminals at 80x24 or larger. Narrow terminals,
resize, `NO_COLOR`, and `TERM=dumb` have plain-text behavior. If the locale cannot
encode Unicode, configure a UTF-8 locale such as `C.UTF-8` before starting J.A.R.N.

## Support tiers

Tier 1 means automated clean-install, upgrade, rollback, uninstall, CLI startup,
preserved-data reinstall, and compatibility tests on the exact OS/CPU row are
release gates. Compatibility tier means the installer contract is explicit and
the environment is expected to work, but a real lifecycle on that host is not a
mandatory per-release gate. Other Unix-like environments may work from source,
but failures there are not release blockers and the official installer refuses to
claim readiness.

See [Troubleshooting](TROUBLESHOOTING.md) for GLIBC, PATH, proxy, and permission
diagnostics and [Known limitations](KNOWN_LIMITATIONS.md) for deliberate boundaries.
