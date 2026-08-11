# Configuration migration and recovery

J.A.R.N. configuration is versioned with the top-level `config_version` field.
The current schema is version 3. Files from supported earlier releases (versions
0, 1, and 2) are migrated through every intermediate schema before use.

## Transaction contract

An on-disk migration follows this order:

1. Lock the configuration and read the original bytes.
2. Parse it and reject corrupt YAML or a schema newer than this executable.
3. Build the complete migration in memory and validate the final schema.
4. Verify that the source has not changed since planning.
5. Write a timestamped, byte-for-byte backup beside the source, such as
   `config.yaml.bak.20260809T104500.123456Z`.
6. Write and fsync an adjacent temporary file, validate it again, and atomically
   replace the original.
7. Re-read the installed file. If verification fails, restore the backup
   atomically and report failure.

The migrator refuses symbolic-link targets and preserves the original file mode.
Migration never relies on a partially written destination. Concurrent writers are
serialized and a changed source invalidates an older plan.

## Inspect before changing anything

```bash
jarn config path
jarn config validate
jarn doctor --json
jarn doctor --fix --dry-run
```

`jarn config show` prints the selected config file with secret-bearing fields
redacted and appends a provenance guide. JSON output includes a per-field source
map plus the ordered runtime layers: built-in defaults, global/project config,
environment references, CLI flags, and preset/trust policy. It does not pretend
that one raw file is the fully merged runtime config. `jarn config path`
identifies the selected scope. `jarn doctor --fix --dry-run` shows allowlisted
repairs, including a required schema migration, without writing.

To apply safe repairs explicitly:

```bash
jarn doctor --fix
```

The command prints the created backup and the installed schema. It must return
nonzero if either activation or post-write validation fails.

## Corrupt or unsupported files

- **Corrupt YAML:** the source stays unchanged. Correct it manually or restore a
  reviewed timestamped backup.
- **Invalid values:** use `jarn config validate` to locate the setting. Migration
  does not discard a value merely to make validation pass.
- **Legacy inline credential:** first move the value to an environment variable,
  OS keychain, or J.A.R.N.'s mode-`0600` secret file, then replace the YAML value
  with the corresponding reference. GA validation never echoes or automatically
  relocates plaintext credentials.
- **Newer schema:** upgrade J.A.R.N. Do not downgrade and rewrite the file.
- **Interrupted migration:** the original or fully validated replacement remains;
  an adjacent temporary file is never accepted as configuration.
- **Permission failure:** correct file/directory ownership or mode, then rerun the
  dry-run. Do not use `sudo jarn` against a user installation.

To restore a backup, first preserve the current file, inspect both files, then copy
the chosen backup to the path printed by `jarn config path` using an atomic editor
or `jarn doctor` repair when offered. J.A.R.N. never chooses among multiple backups
silently.

## Setup reruns and customization

Running setup again updates only the settings selected in that setup transaction.
Custom providers, routing, permission rules, hooks, MCP servers, extensions, and
supported extension namespaces must survive. Configuration is committed only after
the chosen authentication and model are verified; a cancelled or failed setup keeps
the prior configuration.

`jarn config reset` is destructive and therefore requires an itemized preview and
explicit confirmation. It creates a recovery backup and does not erase sessions or
credentials. Use `jarn uninstall` for separately controlled data removal.

See [Configuration](CONFIGURATION.md) for the schema and
[Troubleshooting](TROUBLESHOOTING.md#corrupt-or-unreadable-configuration) for recovery.
