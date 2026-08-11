# Known limitations

These are deliberate or unresolved boundaries of the current release candidate.
They must not be hidden by setup or installer success messages.

- Native Windows is not supported. Use WSL2 Ubuntu; there is no PowerShell installer.
- Alpine/musl and Linux distributions outside the documented Ubuntu/Debian matrix
  are unsupported.
- Intel macOS uses the isolated managed-Python installation path rather than a native
  release binary.
- ChatGPT subscription mode depends on a compatible official Codex CLI and its local
  app-server protocol. Setup cannot finish that path until dependency, login, account,
  and selected-model verification all succeed.
- Browser login may be inappropriate over SSH, in a container, or without a desktop.
  Device login is selected for those environments and still requires completing the
  displayed URL/code on another device.
- A model catalog can be live, cached, stale, or an explicit offline fallback. A
  fallback list is not proof that the current account can use a model; its provenance
  is shown and selection is revalidated before a turn.
- Provider APIs, model entitlements, quotas, and subscriptions are controlled by the
  provider and can change independently of J.A.R.N. A plan name is never treated as
  model entitlement proof.
- J.A.R.N. runs tools on the host by default. Its permission engine and danger guard
  reduce risk but do not make arbitrary shell commands harmless. Use the OS sandbox
  or Docker backend where stronger isolation is required.
- Linux network isolation depends on available platform facilities. Filesystem
  sandbox support does not automatically imply complete network isolation.
- Rollback is available only when a prior verified installer-managed version was
  retained. Legacy package-manager installations may need that manager's rollback.
- Project-local `.jarn/` state belongs to the project and is not removed by global
  uninstall.
- Local telemetry is off by default and never uploaded. Separately enabled LangSmith
  or OpenTelemetry tracing can transmit richer data to its configured destination.
- The repository may contain unreleased GA work. Documentation describing a release
  candidate is not a claim that a GitHub/PyPI/npm GA artifact has been published.

For supported targets see [Supported platforms](SUPPORTED_PLATFORMS.md). For
actionable recovery steps see [Troubleshooting](TROUBLESHOOTING.md).
