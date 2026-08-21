# Quickstart: install to first prompt

This is the single recommended path for a new user. It requires no `sudo`, Node.js,
Python, uv, manual PATH edit, API key, or prior Codex installation. You need a
supported terminal host, `curl`, network access, and a ChatGPT plan that can use
Codex.

## 1. Run one command

Copy the whole line:

```bash
jarn_installer_tmp=$(mktemp "${TMPDIR:-/tmp}/jarn-install.XXXXXX") && trap '[ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"' 0 HUP INT TERM && curl -fsSL 'https://raw.githubusercontent.com/chayapats/jarn/main/install.sh' -o "$jarn_installer_tmp" && sh "$jarn_installer_tmp"; jarn_install_rc=$?; [ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"; trap - 0 HUP INT TERM; if [ "$jarn_install_rc" -eq 0 ] || [ "$jarn_install_rc" -eq 10 ]; then exec "$SHELL" -l; else (exit "$jarn_install_rc"); fi
```

The line downloads to a secure temporary file and never executes it if `curl`
fails. The installer then detects platform/libc, inventories every existing `jarn`
command, downloads one exact release, verifies integrity, smoke-tests it, activates
it transactionally, and verifies the command a fresh shell will actually resolve.
The previous working command is retained until all activation checks pass.

## 2. Choose “Continue with ChatGPT”

The first screen has four simple choices, plus a clearly separated Advanced path:

1. Continue with ChatGPT
2. Use OpenCode Go
3. Use another cloud provider
4. Use a local model

Choose **Advanced** only for custom endpoints/provider registry entries, separate
main/subagent/summarizer routes, reasoning effort, fallbacks, budgets, theme, or
permission profile.

Choose ChatGPT. If a compatible official Codex CLI is missing or outdated, J.A.R.N.
shows its official source, version, and user-space destination and asks to install or
update it. The dependency candidate must pass checksum, executable, and app-server
handshake checks before setup continues.

## 3. Complete visible login

On a desktop, J.A.R.N. prints the URL and opens the browser. Over SSH, in a container,
or without a display, it automatically uses device login and prints the verification
URL, one-time code, and expiry. Complete that ceremony in a browser and keep the
terminal open.

Setup does not infer success from a child-process exit code. It waits for the Codex
app-server completion event, refreshes account state, and verifies managed ChatGPT
mode and usable account metadata. API-key mode is not accepted as subscription mode.

## 4. Accept the live default model

J.A.R.N. requests the full paginated model catalog for the authenticated account,
hides unavailable/hidden entries, and recommends the provider default. Pick a
reasoning level offered for that exact model, or accept its default. Catalog source
is labeled live, fresh cache, stale cache, local discovery, or unverified fallback.

Configuration is committed only after dependency, auth, model, and write validation
succeed. If you cancel or a stage fails, setup remains resumable and the previous
configuration stays intact.

## 5. Ask the first question

The verified completion summary shows the active J.A.R.N. path/version, install
method, auth state, provider/model, reasoning level, permission mode, and working
directory. Then:

```bash
cd /path/to/your/project
jarn
```

At the prompt, try:

```text
Explain this repository and suggest the safest first improvement.
```

The default asks before changes. Use `/status` to see the current directory, model,
auth/provider, reasoning, permission mode, and session; use `/help` for commands.

## Help works before setup and offline

`jarn --help` neither creates configuration nor contacts a provider, launches a
browser, or starts the interactive UI. It is the self-contained command map for a
clean or offline machine: install/path and setup-state inspection, authentication,
model and reasoning selection, permission safety, diagnosis and repair, support
reports, update, rollback, and itemized uninstall are all shown there. Use
`jarn <command> --help` for that command's flags.

## If setup does not reach a verified summary

Do not treat `Done`, an exit-zero child command, or a healthy absolute-path binary as
readiness. Run:

```bash
jarn auth status
jarn doctor
jarn doctor --fix --dry-run
```

Then follow the exact action and stable error code. Common cases are covered in
[Troubleshooting](TROUBLESHOOTING.md), including GLIBC mismatch, old commands on PATH,
device login, billing-mode mismatch, stale catalogs, proxy/CA, and permissions.

## Other supported paths

- **API key:** choose **Anthropic** or **Another cloud provider**. The key is entered
  without echo and stored in the OS keychain or permission-restricted fallback;
  config stores only a reference.
- **Ollama or LM Studio:** choose “Use a local model.” Healthy endpoints and models
  are discovered before any cloud-key prompt. For Ollama, the standard picker also
  verifies `/api/show` reports the `tools` capability; completion-only models are
  shown as incompatible instead of allowing setup to false-succeed.
- **Advanced providers:** choose Advanced only when you need Anthropic, OpenRouter,
  OpenAI-compatible endpoints, custom routing, or the broader provider registry.

Package-manager and source installations are documented as advanced alternatives in
the main [README](../README.md). Platform boundaries are in
[Supported platforms](SUPPORTED_PLATFORMS.md).
