# J.A.R.N. — Improvement Plan

**Author:** code review synthesis (5 parallel subsystem reviews + manual spot-checks)
**Date:** 2026-07-01
**Scope:** whole codebase (`src/jarn/`, tests, docs, packaging, scripts, CI)
**Goal (per owner):** improve the project *overall* — security, correctness, maintainability, features, and hygiene — not narrowly scoped to one axis.
**Threat model (per owner):** running *untrusted* repos is a **secondary** use case. We harden the real exploitable gaps, but we do **not** pivot to "sandbox-by-default for everyone" or build a full shell AST parser. The danger-guard stays a defense-in-depth *net*, with honest documentation of its limits.

---

## Branch & PR strategy (T-0-2)

- **Working branch:** `improve/phase-1-security` (off `main`).
- **One PR per task** (or per tightly-coupled task group within a phase); PR title prefixed `phase-N: …`; squash-merge.
- Within this branch, work is committed **per task** (`phase-1: <task-id> <summary>`) so each task is individually reviewable and revertable.
- **Docs follow code:** any user-visible change updates README/CHANGELOG/SECURITY/ROADMAP in the same commit/PR.

## Green baseline (T-0-1)

Captured on `improve/phase-1-security` at branch creation (off `main` @ `305e073`):

| Check | Result |
|---|---|
| `uv run ruff check src tests scripts` | **clean** — all checks passed |
| `uv run mypy src/` | **clean** — no issues found in 76 source files |
| `uv run pytest --collect-only -q` | **1183 tests collected** |
| `uv run pytest -q` | **1150 passed, 8 skipped, 10 failed, 15 errors** |

The 25 failures/errors are exclusively the **git-subprocess suites** (`test_checkpoint`, `test_git_commands`, and the undo/redo/abort cases in `test_controller`/`test_repl`/`test_wave_b_wiring`/`test_repomap::test_cache_second_call_uses_cache`) that fork `git` and cannot run inside this local restricted sandbox. **GitHub Actions CI is the source of truth** for those; locally they are expected-to-fail and must not regress *in kind* (no new non-subprocess failures). Every subsequent PR references these numbers.

---

## How to read this plan

- **Phase** = a coherent batch with its own goal and a consolidated Definition of Done.
- **Task** = one shippable change: `T-<phase>-<n>`. Each task lists Problem, Files, Action steps, DoD (checklist), Tests, Risk, Effort.
- **Effort** is T-shirt: `S` ≤ 0.5 day, `M` ≈ 1–2 days, `L` ≈ 3–5 days, `XL` ≥ 1 week.
- **Severity** in findings: `critical / high / medium / low`.
- Phases are ordered by risk reduction, not by ease. Phases 1–2 are sequenced; Phases 3–4 can partly overlap.
- Every task ends with a "verify" step — this plan assumes verification-before-completion discipline (run tests, lint, mypy, and the relevant targeted test before marking done).

---

## Guiding principles

1. **Behavior-preserving refactors first within a phase** — splits (Phase 3) must keep the test suite green at every commit; no big-bang rewrites.
2. **Security fixes ship with tests** — each hardening task adds a regression test that fails before the fix.
3. **Fail closed, fail loud** — when in doubt, refuse + emit a clear message instead of silently degrading.
4. **Don't ship half-promises** — if a flag/feature is exposed but unwired, either wire it or remove it in the same phase.
5. **Docs follow code** — any user-visible change updates README/CHANGELOG/SECURITY/ROADMAP in the same PR.

---

## Phase 0 — Pre-flight (setup & baselines)

**Goal:** establish a safe, repeatable working baseline before any change.

### T-0-1 — Capture the green baseline
**Problem:** 25 tests fail locally in a restricted sandbox (git-subprocess tests); CI on GitHub Actions is the source of truth. We need a known-green reference.
**Files:** none (operational)
**Action:**
1. Create a working branch `improve/phase-1-security` off the current default branch.
2. On a machine with `git` available (or trust CI), run `uv sync --extra dev && uv run ruff check src tests scripts && uv run mypy src/ && uv run pytest -q` and record pass/skip/fail counts.
3. Record the authoritative **collection count** via `uv run pytest --collect-only -q | tail -1` (currently **1183**, docs say 1166 — drift tracked in T-4-9).
**DoD:**
- [ ] Baseline numbers (passed/skipped/failed, collection total) written into the PR description of the first change.
- [ ] `ruff` and `mypy` are clean on the base branch.
**Tests:** none
**Risk:** low
**Effort:** S

### T-0-2 — Agree on branch/PR strategy
**Problem:** the plan touches many files; one giant PR is unreviewable.
**Files:** none
**Action:** one PR per task (or per tightly-coupled task group within a phase); each PR title prefixed `phase-N: …`; squash-merge.
**DoD:**
- [ ] Strategy documented at the top of this file (this section).
**Tests:** none
**Risk:** low
**Effort:** S

### Phase 0 — Definition of Done
- [ ] Baseline captured and referenced from every subsequent PR.
- [ ] Branch + PR conventions agreed.

---

## Phase 1 — Security & Integrity

**Goal:** close exploitable gaps that can leak secrets, destroy config, or let an untrusted/compromised project escalate cost or run destructive commands. Untrusted repos are *secondary*, so we close the real holes without building a sandbox-first redesign.

**Scope (13 tasks):** guard hardening, central redaction, inline-key rejection, YAML integrity, scope-check fix, untrusted sanitization, trust TOCTOU, hook hardening, preset safety, secret-file perms, provider `extra` restriction, load-time URL/event validation, `JARN_HOME` threat doc.

### T-1-1 — Harden the danger-guard (patterns + bypass classes)
**Problem:** `src/jarn/permissions/guard.py` is a regex net over the pre-shell string; `shell=True` execution downstream means several classes bypass it. Verified gaps: `rm -rf ${HOME}` (brace form) escapes the root-target BLOCK; missing patterns for `eval`/`bash -c`/`python -c`/heredoc bodies/`$(printf …)`/base64-decoded payloads; missing package-manager postinstall vectors (`npm install`, `pnpm install`, `yarn add`, `pip install`, `uv pip install`, `npx`, `bunx`); missing `docker run --privileged`, `diskutil eraseDisk`, `shutdown`/`reboot`, `truncate -s 0`, `git checkout .` / `git restore .` (mass discard), `curl -o file; sh file`, `base64 -d | bash`; flag-order bypass (`chmod 777 -R .` may slip past `\bchmod\s+-R\b`); no Unicode/homoglyph normalization; `find -exec rm` not caught (only `-delete`).
**Files:** `src/jarn/permissions/guard.py`, `tests/test_guard.py`
**Action:**
1. Fix `_ROOT_TARGET` to also match `${HOME}` / `${HOME}/...` and `~` expansions.
2. Add DANGEROUS/BLOCKED rules: `npm install|pnpm install|yarn add` → DANGEROUS (postinstall); `pip install|uv pip install|uv add|npx|bunx` → DANGEROUS; `docker run` with `--privileged` or `--pid=host` or `--net=host` → BLOCKED; `diskutil eraseDisk`/`mkfs` already BLOCKED, add `shutdown`/`reboot`/`halt` → DANGEROUS; `truncate -s 0` on a non-empty target → DANGEROUS; `git checkout .`/`git restore .`/`git checkout -- *` → DANGEROUS (mass discard); `base64 -d ... | sh` and `curl/wget -o <file> ...; sh <file>` → DANGEROUS; `find ... -exec rm`/`-execdir rm` → DANGEROUS.
3. Normalize flag-order for `chmod`/`chown` (detect `-R` anywhere in the argv, not only immediately after the verb).
4. Add NFKC Unicode normalization of the command string before matching (homoglyph defense), with a comment that this is best-effort.
5. Add a *documented* note in `SECURITY.md` and `docs/PERMISSIONS.md`: the guard is a **net, not a sandbox**; chaining via `eval`/`bash -c`/interpreters can hide payloads; for untrusted code use `execution.backend: docker` or the OS sandbox. Do **not** claim completeness.
6. Keep `! cmd` REPL escape semantics (intentional bypass) but make the REPL print a one-line reminder that the guard is skipped for `!`.
**DoD:**
- [ ] `rm -rf ${HOME}` and `rm -rf -- /*` classify as BLOCKED.
- [ ] Every new pattern has a passing test in `test_guard.py` and a failing test on the pre-fix code.
- [ ] `chmod 777 -R .` and `chmod -R 777 .` both classify DANGEROUS.
- [ ] `npm install`, `pip install`, `npx create-app`, `docker run --privileged …`, `git restore .`, `find . -exec rm -rf {} +` all classify DANGEROUS or BLOCKED as specified.
- [ ] Unicode-homoglyph `rm` (Cyrillic `m`) is normalized and matched.
- [ ] `SECURITY.md` + `docs/PERMISSIONS.md` updated with the honest limits paragraph.
**Tests:** extend `test_guard.py` with one case per new pattern + bypass class + a homoglyph case.
**Risk:** over-broad patterns may flag benign commands (e.g. `npm install` in a trusted workflow). Mitigation: DANGEROUS (not BLOCKED) for installers so they remain usable with confirmation; document the trade-off.
**Deps:** none
**Effort:** M

### T-1-2 — Central secret redaction across transcripts + logs + errors ✅
**Problem:** `src/jarn/config/secrets.py` has no central redactor. `memory/sessions.write_tool()` does not redact tool `result` or arg string values; `observability/logging.py` has no redaction filter. Resolved API keys in tool output can persist to `.jarn/sessions/*.jsonl` and `jarn.log`.
**Files:** `src/jarn/config/secrets.py`, `src/jarn/memory/sessions.py`, `src/jarn/observability/logging.py`, callers
**Action:**
1. Add `redact_secrets(value: str, *, known: set[str] | None = None) -> str` in `secrets.py` (single source of truth). Accept an optional set of live secret values to scrub (in addition to pattern-based: `sk-…`, `sk-proj-…`, `Bearer …`, PEM blocks `-----BEGIN … PRIVATE KEY-----`, long base64 blobs).
2. Refactor `sessions.redact_secrets` to delegate to the central helper (keep the old name as a thin alias for back-compat).
3. In `write_tool()`, run `redact_secrets` over `result` (string form) and over each string value in `args` before serializing.
4. Add a `logging.RedactingFilter` that scrubs records using the central helper; attach it to the rotating handler in `logging.py`.
5. In error formatters that interpolate `{spec!r}` / `{exc}` (e.g. `models.py`, `secrets._resolve_keychain`), pass messages through `redact_secrets`.
**DoD:**
- [x] A transcript round-trip test confirms a synthetic `sk-…` key placed in a tool `result` and in an arg value is masked (`sk-…XXXX`) in the JSONL line.
- [x] A log capture test confirms a key logged via `logger.info` is masked in the log output.
- [x] Existing `test_secrets.py` / `test_transcript.py` still pass; `redact_secrets` has unit tests for every pattern.
**Tests:** `test_secrets.test_redact_*`, `test_transcript.test_tool_redaction`, `test_observability.test_log_redaction`.
**Risk:** redaction could mask a value the user actually needs in output. Mitigation: only mask high-entropy patterns + explicit known-secret set; keep first/last 4 chars for sk-style keys.
**Deps:** none
**Effort:** M

### T-1-3 — Reject/warn on inline plaintext `api_key` at load
**Problem:** `secrets.resolve()` returns non-reference literals verbatim, so inline keys in `config.yaml` work — contradicting "referenced, never inlined" docs. Keys sit in memory and can leak via `repr`/crash dumps.
**Files:** `src/jarn/config/loader.py`, `src/jarn/config/secrets.py`, `tests/test_config.py`, `tests/test_secrets.py`
**Action:**
1. In `loader._build_providers`, after resolving each provider's `api_key`: if `is_reference(ref) is False` **and** the literal matches a secret pattern (`sk-…`, `Bearer …`, ≥32 high-entropy chars, PEM block), emit a **warning** by default and an **error** when a strict mode flag is set (`config.strict_secrets: true`, default false for back-compat).
2. Expand `secrets` secret-pattern set (used by both redaction and this check) to include `sk-proj-…`, `Bearer …`, PEM blocks, GitHub PATs (`ghp_…`), GitLab tokens (`glpat-…`).
3. Update `docs/CONFIGURATION.md` and `SECURITY.md` to state inline keys are discouraged and, in strict mode, rejected.
**DoD:**
- [ ] Config with `api_key: sk-live-xxxx…` loads with a visible warning (non-strict) and is rejected with a clear error in strict mode.
- [ ] Local providers (Ollama base URL token, empty key) still pass without warning.
- [ ] Docs updated.
**Tests:** `test_config.test_inline_api_key_warns`, `test_config.test_inline_api_key_strict_rejects`, `test_config.test_local_provider_no_warn`.
**Risk:** existing users with inline keys get a noisy warning. Mitigation: warning only by default; provide a one-line migration hint ("move to `keychain:` or `${ENV}`").
**Deps:** T-1-2 (shares the pattern set)
**Effort:** S

### T-1-4 — Fix YAML-corruption → config wipe (fail closed + backup)
**Problem:** `config/settings.ConfigStore._load()` returns `{}` on any YAML/OS error; the next `set()` atomically overwrites a valid `config.yaml` with a near-empty dict — silent loss of providers/hooks/MCP. Same pattern in `permissions/rule_store.py` L60-67.
**Files:** `src/jarn/config/settings.py`, `src/jarn/permissions/rule_store.py`, `tests/test_settings.py`, `tests/test_permissions.py`
**Action:**
1. On parse/OS error, raise a typed `ConfigCorruptError` instead of returning `{}`; do **not** write.
2. Before any atomic write, copy the current file to `<path>.bak` (keep last 2 backups).
3. If the file is missing entirely, allow first-write with an empty dict (legitimate bootstrap) — distinguish "missing" from "corrupt".
4. Surface a user-facing repair hint in the error ("Run `jarn doctor`; a backup was saved at …").
**DoD:**
- [ ] A test that corrupts `config.yaml` then calls `set()` proves the file is **not** overwritten and a `.bak` exists and a `ConfigCorruptError` is raised.
- [ ] Same for `rule_store` allow-rule persistence.
- [ ] Missing-file first-write still works (bootstrap test).
**Tests:** `test_settings.test_corrupt_config_not_wiped`, `test_permissions.test_corrupt_rule_store_not_wiped`, `test_settings.test_missing_file_bootstrap`.
**Risk:** low
**Deps:** none
**Effort:** S

### T-1-5 — Fix scope-check CWD bug + symlink handling
**Problem:** `permissions/engine.py` L182-190 uses `Path(target).resolve()`, which resolves relative paths against the **process CWD**, not `project_root`. An agent in a subdir writing `../outside` can be mis-classified as in-scope in `auto-edit`/`yolo`.
**Files:** `src/jarn/permissions/engine.py`, `tests/test_permissions.py`
**Action:**
1. Resolve relative targets against `project_root`: `(project_root / target).resolve()`, then check `project_root in resolved.parents` (or equality).
2. For symlinks: resolve the real target but also reject writes where the symlink points outside `project_root` (TOCTOU mitigation: check at write time is already the engine's job; add a clear note that the engine checks intent, the tool layer enforces at syscall time).
3. Add `..` traversal tests and a symlink-escape test.
**DoD:**
- [ ] `evaluate(write, "../outside")` from a CWD deeper than `project_root` is DANGEROUS/BLOCKED, not allowed.
- [ ] Symlink pointing outside `project_root` is rejected for writes.
- [ ] Normal in-project writes still allowed in `auto-edit`.
**Tests:** `test_permissions.test_scope_cwd_independence`, `test_permissions.test_symlink_escape`.
**Risk:** low
**Deps:** none
**Effort:** S

### T-1-6 — Complete untrusted-project sanitization (allowlist for project tier)
**Problem:** `config/trust.py` `DANGEROUS_TOP_KEYS` strips only `providers/hooks/mcp_servers/observability`. Untrusted projects can still merge `routing`, `budget` (`per_session_usd: 0` disables caps), `wiki.enabled`, `compat`, `default_model`, `git`, `plan`, `context` while using the user's global credentials → cost abuse + behavior change with no trust prompt.
**Files:** `src/jarn/config/trust.py`, `src/jarn/config/loader.py`, `tests/test_trust.py`
**Action:**
1. Switch untrusted-project sanitization from a blocklist to an **allowlist** of safe project-tier keys: `permissions` (deny rules only — already safety-increasing), `ui` (cosmetic), and a short explicit safe set. Everything else is dropped when `project_trusted=False`.
2. Explicitly drop `routing`, `budget`, `wiki`, `compat`, `default_model`, `git`, `plan`, `context` for untrusted projects (document that the user must `jarn trust` to enable them).
3. Add a `jarn doctor` line showing which project keys were stripped for transparency.
4. Update `docs/PERMISSIONS.md` "Untrusted repos" section.
**DoD:**
- [ ] `test_trust` asserts each of `routing/budget/wiki/compat/default_model/git/plan/context` is stripped when untrusted.
- [ ] Existing trusted-repo behavior unchanged.
- [ ] `doctor` output lists stripped keys for an untrusted repo.
**Tests:** expand `test_trust.py` with one assertion per newly-stripped key.
**Risk:** projects that legitimately set `routing`/`budget` in `.jarn/config.yaml` will require trust before those take effect — expected and documented.
**Deps:** none
**Effort:** M

### T-1-7 — Fix TOCTOU on `/trust` (single read)
**Problem:** `trust.py` takes the fingerprint from `_read_yaml` then runs a **separate** `load_config()`; disk can change between reads → stored fingerprint may not match the loaded config.
**Files:** `src/jarn/config/trust.py`, `tests/test_trust.py`
**Action:**
1. Read the project YAML **once**; derive the fingerprint from that exact bytes object and pass the parsed/merged config forward (or re-verify the fingerprint against a second read and refuse if mismatched).
2. After merge, optionally re-hash the merged result and store both for audit.
**DoD:**
- [ ] A test that mutates the project config between the two reads (simulated) is refused with a clear "config changed during trust" error.
**Tests:** `test_trust.test_toctou_refuses_on_mid_read_change`.
**Risk:** low
**Deps:** T-1-6
**Effort:** S

### T-1-8 — Harden lifecycle hooks (shell, env, global-hook trust)
**Problem:** `extensibility/hooks.py` runs `subprocess.run(..., shell=True)` with full `os.environ` merged in. Project hooks are trust-gated, but **global** hooks in `~/.jarn/config.yaml` always run. Shell injection if global config is compromised; secrets in env leak to hook subprocesses. `HookEvent` typos silently no-op. `controller._fire_lifecycle` wraps `runner.run` in `suppress(Exception)` → failures silent.
**Files:** `src/jarn/extensibility/hooks.py`, `src/jarn/tui/controller.py`, `src/jarn/config/loader.py`, `docs/EXTENDING.md`
**Action:**
1. Validate `HookSpec.event` against an allowed-events enum at load (fail with `ConfigError` on typo).
2. Build the subprocess env from a **minimal allowlist** (e.g. `PATH`, `HOME`, `JARN_*`, plus explicitly-declared `extra_env`) instead of inheriting all of `os.environ`. Provide a config opt-in `hooks.inherit_env: true` for users who want the old behavior.
3. Optionally require `hooks.global_require_trust: true` (config flag, default false for back-compat) so global hooks also get a one-time accept prompt.
4. Stop swallowing hook failures silently in `_fire_lifecycle`: log at WARNING and, for blocking events, surface a user-visible notice (non-fatal) instead of `suppress(Exception)`.
5. Document the threat model in `docs/EXTENDING.md` (hooks run shell on the host; only trust repos / your own global config).
**DoD:**
- [ ] Test: a hook with a typo'd event is rejected at load.
- [ ] Test: hook subprocess does **not** see `OPENROUTER_API_KEY` unless explicitly passed via `extra_env`.
- [ ] Test: a failing `pre_commit` hook logs a WARNING and surfaces a notice instead of being swallowed.
**Tests:** `test_extensibility.test_hook_event_validation`, `test_extensibility.test_hook_env_allowlist`, `test_extensibility.test_hook_failure_surfaced`.
**Risk:** users relying on env inheritance in hooks need to declare `extra_env`. Mitigation: clear migration note + `hooks.inherit_env: true` escape hatch.
**Deps:** none
**Effort:** M

### T-1-9 — Make the `ci` preset safe-by-default
**Problem:** `config/profiles.py` `ci` preset = `permission_mode: yolo` + `local_sandbox: require`. If launched on a CI host where the sandbox runtime is unavailable, YOLO + pattern-only guard is risky.
**Files:** `src/jarn/config/profiles.py`, `tests/test_preset_unify.py`, `docs/CONFIGURATION.md`
**Action:**
1. Change `ci` preset to `permission_mode: ask` + an explicit allowlist, **or** keep `yolo` but require `execution.backend: docker` (fail launch with a clear error if docker is unavailable).
2. Document in `docs/CONFIGURATION.md` that `ci` is only safe with the docker backend.
**DoD:**
- [ ] `ci` preset no longer silently runs YOLO on a host without a sandbox.
- [ ] Test asserts the preset's safety constraint.
- [ ] Docs updated.
**Tests:** update `test_preset_unify.py` / `test_profiles.py`.
**Risk:** existing CI users on YOLO need to adjust. Mitigation: keep a `ci-yolo` preset name for opt-in dangerous behavior, documented.
**Deps:** none
**Effort:** S

### T-1-10 — Tighten secret-file permissions + keychain read-path validation
**Problem:** `secrets._store_file_secret` sets `0600` on the file and `0700` on the immediate parent only; `~/.jarn` may remain `755`. `keychain:` `service` is not validated with `_ACCOUNT_RE` on the read path (only the file store validates).
**Files:** `src/jarn/config/secrets.py`, `tests/test_secrets.py`
**Action:**
1. Ensure the whole `~/.jarn/secrets/` tree is `0700` (create with mode, `chmod` each existing ancestor up to `~/.jarn/secrets`).
2. Run `_validate_account` on `service` and `account` for `keychain:` references on read, mirroring the file-store path.
**DoD:**
- [ ] Test: after `store_secret` file fallback, `~/.jarn/secrets/` and every ancestor up to it is `0700`, file is `0600`.
- [ ] Test: `keychain:bad/service!` raises on read (invalid account chars).
**Tests:** `test_secrets.test_secret_tree_permissions`, `test_secrets.test_keychain_read_validates_account`.
**Risk:** low
**Deps:** none
**Effort:** S

### T-1-11 — Restrict `ProviderConfig.extra` to a per-provider allowlist
**Problem:** `loader._build_providers` forwards any unknown key in `extra` verbatim to LangChain `init_chat_model`. Malicious/mistyped YAML can inject arbitrary kwargs (headers, URLs).
**Files:** `src/jarn/config/loader.py`, `src/jarn/config/schema.py`, `tests/test_providers_extra.py`
**Action:**
1. Define a per-`ProviderType` allowlist of safe `extra` keys (e.g. `base_url`, `timeout`, `max_retries` for openai_compatible).
2. Reject unknown `extra` keys at load with a `ConfigError` naming the provider and the bad key.
3. For keys that *should* be passable but are sensitive (e.g. custom headers), require them under a typed `ProviderConfig.headers` field that goes through redaction.
**DoD:**
- [ ] Test: a provider with a disallowed `extra` key is rejected at load.
- [ ] Test: allowed `extra` keys (e.g. `base_url`) still reach the provider constructor.
**Tests:** extend `test_providers_extra.py`.
**Risk:** users currently relying on arbitrary `extra` kwargs break. Mitigation: allowlist the common safe ones; ship migration note.
**Deps:** none
**Effort:** M

### T-1-12 — Validate MCP/subagent URLs + hook events at load (early SSRF/spawn guard)
**Problem:** `MCPServer.transport` is a free string; `url`/`command` not validated (SSRF/arbitrary spawn gated only by project trust, not schema). `AsyncSubagentSpec.url`/`headers` not validated at load. Hook event typos silently no-op (overlaps T-1-8).
**Files:** `src/jarn/config/loader.py`, `src/jarn/config/schema.py`, `tests/test_config.py`
**Action:**
1. Validate `MCPServer.transport` ∈ {stdio, http, sse, streamable_http}; validate `url` is an absolute http(s) URL; reject `command` containing shell metacharacters for stdio (the spawn layer should not use `shell=True`).
2. Validate `AsyncSubagentSpec.url` (absolute http(s)) and `headers` (dict[str,str]) at load.
3. Reuse the web_tools SSRF allowlist for MCP HTTP URLs as a defense-in-depth check (warning, not hard block, since MCP endpoints may be internal by design).
**DoD:**
- [ ] Test: invalid `transport`, non-absolute `url`, and `command` with `;`/`|` are rejected at load.
- [ ] Test: invalid subagent `url` is rejected at load.
**Tests:** `test_config.test_mcp_validation`, `test_config.test_subagent_validation`.
**Risk:** low
**Deps:** none
**Effort:** S

### T-1-13 — Document the `JARN_HOME` override threat
**Problem:** `config/paths.py` trusts `JARN_HOME` blindly; a hijacked env (CI, shared shell) can redirect the secrets dir + trust store.
**Files:** `src/jarn/config/paths.py`, `docs/CONFIGURATION.md`, `SECURITY.md`
**Action:**
1. Add an explicit comment + a `jarn doctor` warning when `JARN_HOME` is non-default (so the user sees it).
2. Document the threat in `SECURITY.md`: only set `JARN_HOME` in trusted environments; never inherit it from a cloned project's instructions.
3. (Optional) require an explicit opt-in env (`JARN_ALLOW_CUSTOM_HOME=1`) when the path is non-default and outside `~`.
**DoD:**
- [ ] `doctor` flags a non-default `JARN_HOME`.
- [ ] `SECURITY.md` documents the threat.
**Tests:** `test_settings.test_doctor_warns_custom_jarn_home`.
**Risk:** low
**Deps:** none
**Effort:** S

### Phase 1 — Definition of Done
- [ ] All 13 tasks merged with passing targeted tests.
- [ ] No inline secret can reach a provider without a warning; no secret can persist to transcripts/logs unredacted.
- [ ] Corrupt YAML can never wipe a config file; a `.bak` exists.
- [ ] Out-of-scope writes (CWD-relative / symlink escape) are refused.
- [ ] Untrusted projects cannot override `routing/budget/wiki/…`; `/trust` is TOCTOU-safe.
- [ ] Hooks no longer inherit the full env; hook failures are visible.
- [ ] `guard.py` covers the documented bypass classes; `SECURITY.md` honestly states its limits.
- [ ] Full `pytest -q`, `ruff`, `mypy` green; no behavior regression for trusted-repo users.

---

## Phase 2 — Critical Bugs & Release Safety

**Goal:** kill user-visible bugs and make releases provably safe. Nothing ships to PyPI/npm without passing the same gates as CI.

### T-2-1 — Wire `/clear` (dead `clear_screen` flag)
**Problem:** `controller._cmd_clear` sets `CommandResult.clear_screen=True`; `repl.py` L1432-1437 only checks `rebuilt`/`quit`, so `/clear` never clears scrollback.
**Files:** `src/jarn/repl.py`, `src/jarn/tui/controller.py`, `tests/test_repl.py`
**Action:**
1. Handle `clear_screen` in the REPL command loop: clear the terminal scrollback (or render a visible separator) and invalidate the live region.
2. Add a `/clear` test asserting the screen is cleared (or a separator emitted) and the conversation state reset.
**DoD:**
- [ ] `/clear` visually clears scrollback in an interactive check; test asserts the handler path.
**Tests:** `test_repl.test_clear_clears_screen`.
**Risk:** low
**Effort:** S

### T-2-2 — Implement or remove `--max-turns` (headless honesty)
**Problem:** `headless.py` exposes `--max-turns` but hardcodes `turns=1`; `--json` omits `tool_calls` despite `HeadlessResult` tracking it; all failures collapse to exit 1 with no structured error; refusals in auto-edit/yolo silently return `approved=False` without a distinct exit code.
**Files:** `src/jarn/headless.py`, `src/jarn/cli.py`, `tests/test_headless.py`
**Action:**
1. **Implement** multi-turn headless bounded by `--max-turns` (driver loops until the agent emits a final answer with no further tool requests, or the cap is hit).
2. Add `tool_calls` to the `--json` output.
3. Add structured `--json` error object: `{error: {kind, message}}` for crashes.
4. Distinct exit codes: `0` success, `1` generic error, `2` approval refused / budget hard-stop, `124` timeout.
5. If implementing multi-turn this phase is too big, **remove** `--max-turns` from CLI help and ship the rest (defer multi-turn to Phase 4 task F-1).
**DoD:**
- [ ] Either multi-turn works with a test, or the flag is removed and CLI help is honest.
- [ ] `--json` includes `tool_calls`; errors are structured; exit codes are documented in `--help` and `docs/CONFIGURATION.md` (non-interactive section).
**Tests:** `test_headless.test_multi_turn_cap`, `test_headless.test_json_includes_tool_calls`, `test_headless.test_exit_codes`.
**Risk:** multi-turn headless expands the attack surface for unattended use; keep fail-closed approver behavior.
**Deps:** none
**Effort:** M (implement) / S (remove)

### T-2-3 — Fix checkpoint undo partial-failure (orphan redo + rollback)
**Problem:** `agent/checkpoint.py` ~471-489: if `_capture_redo_point()` succeeds but `_apply_snapshot()` fails, the code restores the undo stack but leaves an orphan on the redo stack → `/redo` confusion. `label` param is unused; stack ref shifting is non-atomic.
**Files:** `src/jarn/agent/checkpoint.py`, `tests/test_checkpoint.py`
**Action:**
1. Roll back the redo-stack push on restore failure (or push redo only after successful apply).
2. Either persist `label` in the commit/ref message or remove the dead param.
3. Add a file lock (`fcntl`/`.jarn-checkpoint.lock`) around stack ref shifts if concurrent `/undo` + turn snapshot is possible from TUI + agent.
4. Use a longer SHA prefix (or full SHA) for snapshot refs to avoid prefix collision.
**DoD:**
- [ ] Test: simulate `_apply_snapshot` failure → redo stack is unchanged (no orphan).
- [ ] Test: concurrent snapshot + undo under the lock does not corrupt the stack.
- [ ] Dead `label` param resolved.
**Tests:** `test_checkpoint.test_undo_rollback_on_apply_failure`, `test_checkpoint.test_concurrent_undo_locked`.
**Risk:** low
**Effort:** M

### T-2-4 — Fix background process FD leak + prune registry
**Problem:** `agent/background.py` L88-101: `log_file` opened and passed to `Popen` is never closed in the parent (FD leak). `_procs` never prunes exited processes (`list_background` grows unbounded). No max lifetime / concurrency cap.
**Files:** `src/jarn/agent/background.py`, `tests/test_background.py`
**Action:**
1. Close the parent-side log FD after `Popen` spawns (child retains its copy).
2. Prune `_procs` entries when `poll() is not None` in `list_background` and `register`.
3. Add optional `background.max_concurrent` and `background.max_lifetime_secs` config (default unlimited for back-compat, warn when exceeded).
**DoD:**
- [ ] Test: after spawning N background jobs and completing them, no parent FDs are leaked (assert via `/proc/self/fd` on Linux or a stub counter cross-platform).
- [ ] Test: `list_background` excludes exited processes.
**Tests:** `test_background.test_no_fd_leak`, `test_background.test_prune_exited`.
**Risk:** low
**Effort:** S

### T-2-5 — Fix repomap O(n²) + unify discovery cache
**Problem:** `agent/repomap.py` L253-270 `_build_xref_counts` is O(files × stems); `build_repo_map()` calls `_discover_files()` (forks `git ls-files` + stats all files) on every tool call instead of using `_discover_with_signature()`'s TTL cache.
**Files:** `src/jarn/agent/repomap.py`, `tests/test_repomap.py`
**Action:**
1. Build a stem→files index once per discovery pass; compute xref counts by iterating files once and looking up stems (linearize).
2. Route `build_repo_map()` through the same TTL-cached discovery as `build_symbol_index`.
3. Improve the cache key beyond `len(files):max_mtime` (include a short tree SHA when in a git repo).
**DoD:**
- [ ] Micro-benchmark/test: xref build on a synthetic 5k-file fixture completes in linear time (assert runtime ratio vs. a 1k fixture ≈ 5×, not 25×).
- [ ] `build_repo_map` no longer forks `git ls-files` per call within the TTL.
**Tests:** `test_repomap.test_xref_linear`, `test_repomap.test_discovery_cache_reused`.
**Risk:** low
**Effort:** M

### T-2-6 — Fix streaming usage double-counting + CostTracker thread safety
**Problem:** `session.py` `_record_usage` runs on every streamed chunk; providers that attach cumulative `usage_metadata` per chunk inflate totals. `CostTracker` has no lock; multi-tool calls attribute cost to the first tool only.
**Files:** `src/jarn/agent/session.py`, `src/jarn/cost/tracker.py`, `tests/test_cost.py`
**Action:**
1. Dedup usage before `CostTracker.record`: track the last seen cumulative `input/output/cache` totals per thread and record only deltas (or record once on the final message).
2. Add a `threading.Lock` around `CostTracker.record` / `status`.
3. For parallel tool calls, attribute cost across the set of tools in the turn (split evenly or by a configurable policy) instead of the first tool only — document the approximation.
**DoD:**
- [ ] Test: a fake provider emitting cumulative usage on 10 chunks yields the **final** totals once, not 10×.
- [ ] Test: concurrent `record` calls don't race (deterministic totals).
**Tests:** `test_cost.test_streaming_usage_dedup`, `test_cost.test_concurrent_record`.
**Risk:** changing attribution may alter displayed per-tool costs; keep a clear note in CHANGELOG.
**Deps:** none
**Effort:** M

### T-2-7 — Fix `models.py` cache invalidation + fallback exception handling
**Problem:** `providers/models.py` `_cache` is never invalidated on `/key` or reload; `fallback_models()` swallows all `Exception` (hides `SecretResolutionError`/auth failures); `list_remote_models` has no auth headers for keyed endpoints (always `[]`).
**Files:** `src/jarn/providers/models.py`, `tests/test_providers_extra.py`
**Action:**
1. Add a `invalidate_cache()` call on `/key` and on config reload; expose via `ModelFactory`.
2. Narrow `fallback_models()` exceptions: re-raise `SecretResolutionError`/`ConfigError`; only swallow transient network errors with a logged warning.
3. Pass auth headers to `list_remote_models` for keyed `openai_compatible` endpoints.
**DoD:**
- [ ] Test: after `/key`, a previously-resolved model re-resolves with the new key (no stale cache).
- [ ] Test: `SecretResolutionError` propagates from `fallback_models` instead of being swallowed.
**Tests:** `test_providers_extra.test_cache_invalidation`, `test_providers_extra.test_fallback_propagates_secret_error`.
**Risk:** low
**Effort:** S

### T-2-8 — Resolve `/compact` naming collision
**Problem:** REPL `/compact` runs interactive compaction; `controller._cmd_compact` only prints auto-compact status and is unreachable from REPL.
**Files:** `src/jarn/repl.py`, `src/jarn/tui/controller.py`, `tests/test_repl.py`, `tests/test_controller.py`
**Action:**
1. Route `/compact` subcommands: `/compact` (apply, interactive) and `/compact status` (show auto-compact state) — single handler, subcommand-aware. (Deeper fix lands with the registry in T-3-3.)
**DoD:**
- [ ] `/compact` and `/compact status` both work and are tested.
**Tests:** `test_repl.test_compact_apply`, `test_controller.test_compact_status`.
**Risk:** low
**Effort:** S

### T-2-9 — Add unified token budgets for memory + wiki + context injection
**Problem:** `memory/context.py` `assemble_system_context`, `memory/store.py` `index_text()`, `memory/wiki.py` `index_text()`, and `context.project_context_text` inject unbounded content into the system prompt (unlike `repo_map_tokens` which is capped).
**Files:** `src/jarn/memory/context.py`, `src/jarn/memory/store.py`, `src/jarn/memory/wiki.py`, `src/jarn/config/schema.py`, `tests/test_memory.py`, `tests/test_wiki.py`
**Action:**
1. Add `context.memory_tokens`, `context.wiki_index_tokens`, `context.project_context_tokens` config (default generous; bounded).
2. Truncate each injected block to its budget with a visible `(truncated N tokens)` notice.
3. Provide a `/context` command (or extend `/cost`) to show the actual injected sizes per source so users can tune.
**DoD:**
- [ ] Test: a 50k-token MEMORY.md is truncated to the configured budget with a notice.
- [ ] Test: wiki index injection respects its budget.
**Tests:** `test_memory.test_index_budget`, `test_wiki.test_index_budget`.
**Risk:** low
**Effort:** M

### T-2-10 — Fix doctor skill-shadowing precedence
**Problem:** `extensibility/skills.py` `skill_dirs()` order contradicts `load_skills()` and its own docstring (`.jarn` vs `.claude`). Doctor uses `skill_dirs()` → reports the wrong "active" skill on `.claude`/`.jarn` collisions.
**Files:** `src/jarn/extensibility/skills.py`, `src/jarn/doctor_extensions.py`, `tests/test_doctor_extensions.py`, `tests/test_extensibility.py`
**Action:**
1. Make `load_skills()` the single source of truth for discovery order; either delete `skill_dirs()` or make it delegate to the same order.
2. Doctor's shadow reporting uses the same loader order.
**DoD:**
- [ ] Test: when `.claude` and `.jarn` both define a skill with the same name, doctor and runtime agree on which is active.
**Tests:** `test_doctor_extensions.test_skill_shadow_matches_runtime`.
**Risk:** low
**Effort:** S

### T-2-11 — Fix `verify.py` false positives
**Problem:** `agent/verify.py` adds `pytest -q` for any `pyproject.toml` (even without pytest configured); `ruff check .` for any pyproject; Node only maps `test`/`build`/`lint` (misses `test:unit`, `check`, `typecheck`); Makefile detection is fragile.
**Files:** `src/jarn/agent/verify.py`, `tests/test_verify.py`
**Action:**
1. Detect pytest only when `[tool.pytest.ini_options]` or a `pytest` dev dep exists; detect ruff only when `[tool.ruff]` exists.
2. Map Node scripts by substring (`test`, `check`, `typecheck`, `lint`, `build`) and read `package.json` scripts keys.
3. Replace Makefile `f"\n{target}:" in text` with a regex on target rules.
**DoD:**
- [ ] Test: a pyproject without pytest does not suggest `pytest -q`; a pyproject with ruff suggests `ruff check`.
- [ ] Test: `test:unit` and `typecheck` scripts are detected.
**Tests:** extend `test_verify.py`.
**Risk:** low
**Effort:** S

### T-2-12 — `/review` includes untracked new files
**Problem:** `agent/git_commands.py` `/review` uses `git diff` only → untracked new files are invisible.
**Files:** `src/jarn/agent/git_commands.py`, `tests/test_git_commands.py`
**Action:**
1. Compose review input from `git diff HEAD` + untracked file contents (via `git diff --no-index /dev/null <file>` or direct read with a cap).
2. Surface stderr snippet on non-zero exit instead of discarding it.
**DoD:**
- [ ] Test: a repo with one modified + one untracked file yields a review prompt that includes both.
**Tests:** `test_git_commands.test_review_includes_untracked`.
**Risk:** low
**Effort:** S

### T-2-13 — Release preflight job (no publish without gates)
**Problem:** `.github/workflows/release.yml` publishes to PyPI/binaries/npm without re-running ruff/mypy/pytest.
**Files:** `.github/workflows/release.yml`, `tests/test_ci.py`
**Action:**
1. Add a `preflight` job that reuses the CI gates (`ruff check src tests scripts`, `mypy src/`, `pytest -q`, `test_packaging.py`); make `pypi`/`binaries`/`npm` jobs `needs: preflight`.
2. Update `tests/test_ci.py` to assert the `needs:` chain exists.
**DoD:**
- [ ] A dry-run (act or workflow YAML lint) shows publishes can't start before preflight passes.
- [ ] `test_ci` asserts the dependency chain.
**Tests:** extend `test_ci.py`.
**Risk:** low
**Effort:** S

### T-2-14 — CI coverage gate + lint `scripts/` + Windows matrix
**Problem:** `pytest-cov` is a dev dep but CI runs no `--cov` and no `fail_under`; CI lints only `src tests` (RELEASE.md says `src tests scripts`); no Windows/WSL CI.
**Files:** `.github/workflows/ci.yml`, `pyproject.toml`, `tests/test_ci.py`
**Action:**
1. Add `--cov=src/jarn --cov-report=term-missing --cov-fail-under=<N>` to CI (pick `N` from current baseline, then ratchet).
2. Extend lint step to `src tests scripts`.
3. Add `windows-latest` to the matrix (document WSL-only support; allow expected platform skips).
**DoD:**
- [ ] CI runs coverage with a gate; lint covers `scripts`.
- [ ] Windows job runs (with documented skips for unsupported features).
**Tests:** `test_ci` asserts lint scope + coverage flag presence.
**Risk:** Windows may surface new failures; isolate as allowed failures initially if needed.
**Deps:** T-2-13
**Effort:** M

### T-2-15 — Add security scanning CI
**Problem:** no dependency-vuln scan, no secret scan in CI.
**Files:** `.github/workflows/ci.yml` (new `security` job), `.github/dependabot.yml`
**Action:**
1. Add `pip-audit` (via `uv pip audit` or `pip-audit`) on the locked env.
2. Add `gitleaks` on the repo.
3. Optionally enable GitHub CodeQL analysis.
4. Add Dependabot config for `uv.lock` (pip ecosystem) + npm.
**DoD:**
- [ ] Security job runs on PRs; fails on high-severity vulns / leaked secrets.
- [ ] Dependabot config present.
**Tests:** `test_ci` asserts the security job exists.
**Risk:** pip-audit may flag transitive deps; triage policy documented.
**Deps:** none
**Effort:** S

### T-2-16 — Add doc-sync test-count enforcement test
**Problem:** docs claim 1166 tests; collection is 1183. CONTRIBUTING mandates the count match but nothing enforces it.
**Files:** `tests/test_doc_sync.py` (new), docs (fix in T-4-9)
**Action:**
1. Add a test that scrapes the test count from README/CONTRIBUTING/RELEASE and compares to `pytest --collect-only` count (run in CI, fast).
**DoD:**
- [ ] Test fails when docs drift; passes after T-4-9 fixes the numbers.
**Tests:** the new test itself.
**Risk:** low
**Deps:** T-4-9 (fix the numbers so the test passes)
**Effort:** S

### T-2-17 — Pin PyInstaller + post-build binary smoke
**Problem:** `scripts/build-binary.sh` installs PyInstaller ad hoc; release workflow doesn't run `./dist/jarn --version` on assembled binaries.
**Files:** `pyproject.toml` (new `build` extra), `scripts/build-binary.sh`, `.github/workflows/release.yml`
**Action:**
1. Add `pyinstaller` to a `build` extra with a pinned version.
2. In `release.yml` binaries job, after build, run `./dist/jarn --version` and `./dist/jarn doctor --json` smoke.
3. In the npm job, run `jarn --version` on the assembled package before publish.
**DoD:**
- [ ] PyInstaller version is reproducible from `uv.lock`.
- [ ] Release job fails if the binary doesn't smoke.
**Tests:** `test_ci` asserts the smoke step; packaging test covers the extra.
**Risk:** low
**Deps:** T-2-13
**Effort:** S

### T-2-18 — Optional nightly live-LLM eval CI
**Problem:** `scripts/eval.py` + `evals/` are manual-only; no regression signal on live models.
**Files:** `.github/workflows/nightly.yml` (new), `evals/README.md`
**Action:**
1. Add a scheduled workflow (optional, guarded by a secret + a cost cap) running `scripts/eval.py` against a small fixture set.
2. Mark it `continue-on-error` or a separate non-blocking status until stable.
**DoD:**
- [ ] Nightly workflow exists, runs on schedule, posts a summary; failure doesn't block PRs.
**Tests:** none (operational)
**Risk:** cost; mitigate with a hard cap + disabled-by-default repo secret.
**Deps:** none
**Effort:** S

### Phase 2 — Definition of Done
- [ ] `/clear`, `--max-turns`/removal, checkpoint undo, background FD, repomap perf, cost dedup, models cache, `/compact`, token budgets, doctor skill precedence, verify, `/review` all fixed and tested.
- [ ] Releases cannot publish without passing CI gates; coverage gate, security scan, and binary smoke exist.
- [ ] Windows + scripts lint in CI; doc-sync test enforces test counts.
- [ ] Full `pytest -q`, `ruff`, `mypy` green.

---

## Phase 3 — Refactor for Maintainability

**Goal:** split the four oversized files and collapse duplicated logic, **without changing behavior**. Every commit stays green. This unlocks safer feature work in Phase 4.

**Rule:** each split lands as a series of mechanical moves (move code, fix imports, re-run tests) — no logic changes in the same commit. If a behavior change is needed, it's a separate task.

### T-3-1 — Split `repl.py` → `jarn/repl/` package
**Problem:** `repl.py` ~2,311 lines.
**Files:** new `src/jarn/repl/{__init__,app,keys,overlays,commands,turn,completer,auth_errors}.py`; delete old `repl.py`; keep `repl_renderer.py` as-is.
**Action:** move by region per the structure map:
- `__init__.py` → `run_inline()`
- `app.py` → `InlineApp` lifecycle, layout, stream region
- `keys.py` → `_build_keys()`
- `overlays.py` → pager, config panel, menus, `_ask`/`_pick_*`
- `commands.py` → `_command` dispatch + REPL-only `/queue /resume /rewind /compact`
- `turn.py` → `_run_turn`, `_approve*`, `_edit_text_in_editor`
- `completer.py` → `_ShellEscapeLexer`, `_SlashFileCompleter`
- `auth_errors.py` → `_provider_hint`, `_friendly_auth_error`
**DoD:**
- [ ] `repl.py` deleted; `jarn.repl` importable; `jarn` entry point unchanged.
- [ ] `test_repl.py` (~98 tests) passes with no edits beyond import paths.
- [ ] No file in `jarn/repl/` exceeds ~600 lines.
**Tests:** existing `test_repl.py` unchanged in behavior.
**Risk:** import cycles; mitigate by keeping `controller` imports at function level where needed.
**Deps:** none
**Effort:** L

### T-3-2 — Split `controller.py` → `jarn/controller/` package
**Problem:** `controller.py` ~2,044 lines; `handle_command` uses `getattr(self, f"_cmd_{name}")` (typo → runtime miss).
**Files:** new `src/jarn/controller/{__init__,core,doctor_render}.py` + `commands/{__init__,config,session,memory,diagnostics,meta}.py`.
**Action:**
1. `core.py` → lifecycle, runtime, model rotation, context, isolation (~750 lines).
2. `commands/*.py` → grouped `_cmd_*` handlers.
3. `commands/__init__.py` → explicit `REGISTRY: dict[str, CommandHandler]` with metadata (description, interactive-only).
4. `doctor_render.py` → shared with `cli.py` (see T-3-4).
**DoD:**
- [ ] `test_controller.py` (~51 tests) passes unchanged.
- [ ] `handle_command` looks up the explicit registry; unknown commands handled explicitly.
- [ ] No file exceeds ~750 lines.
**Tests:** existing `test_controller.py`.
**Risk:** medium (large move); mitigate with one-commit-per-file and test runs between.
**Deps:** T-3-1 (so the REPL side adapts once)
**Effort:** L

### T-3-3 — Unify slash-command registry (REPL + Controller)
**Problem:** commands live in two layers (REPL if/elif + Controller `getattr`); `/compact` collision; adding a command requires editing both.
**Files:** `src/jarn/repl/commands.py`, `src/jarn/controller/commands/__init__.py`, `tests/test_repl.py`, `tests/test_controller.py`
**Action:**
1. Single `REGISTRY` with `CommandSpec(name, description, layer: "ui"|"core"|"both", handler)`.
2. REPL dispatches UI/both handlers; Controller dispatches core/both. Hyphenated names normalized once.
3. Generate `/help` from the registry so it can't drift.
**DoD:**
- [ ] Adding a new command touches one file (the registry) for metadata + one handler file.
- [ ] `/help` is generated from the registry; a test asserts it matches the README command table (extend `test_phase3`).
- [ ] `/compact` collision resolved by the registry's subcommand routing.
**Tests:** `test_repl.test_help_generated_from_registry`, `test_phase3.test_commands_match_readme`.
**Risk:** medium
**Deps:** T-3-1, T-3-2
**Effort:** M

### T-3-4 — Extract shared doctor render module
**Problem:** doctor rendering is triplicated (`cli._cmd_doctor`, `cli._print_extensions`, `controller._cmd_doctor` + `_append_doctor_extensions` ~120 lines mirrored); controller imports `jarn.cli` (layer inversion).
**Files:** new `src/jarn/doctor/render.py` (extend `doctor_extensions.py`); `src/jarn/cli.py`; `src/jarn/tui/controller.py`
**Action:**
1. One Rich formatter + one JSON serializer used by `jarn doctor`, `/doctor`, and `jarn doctor --json`.
2. Remove `controller` → `cli` import.
**DoD:**
- [ ] `jarn doctor` and `/doctor` produce identical output (golden test).
- [ ] `--json` path shares the serializer.
- [ ] `controller` no longer imports `jarn.cli`.
**Tests:** `test_doctor_extensions.test_cli_and_command_parity`.
**Risk:** low
**Deps:** T-3-2
**Effort:** S

### T-3-5 — Split `session.py` → events / interrupts / stream_handlers / session
**Problem:** `agent/session.py` 851 lines mixing event types, interrupt resolution, stream chunk handling, and the turn driver.
**Files:** new `src/jarn/agent/{events,interrupts,stream_handlers}.py`; slim `session.py`.
**Action:**
1. `events.py` → `EventKind`, `Event`, approval dataclasses.
2. `interrupts.py` → `_resolve_interrupts`, `_resume_payload`, `_action_requests`.
3. `stream_handlers.py` → message/update chunk handling.
4. `session.py` → `SessionDriver.run_turn` only (~200 lines).
**DoD:**
- [ ] `test_agent_mocked.py` passes unchanged; public `SessionDriver` API unchanged.
- [ ] `session.py` ≤ ~250 lines.
**Tests:** existing `test_agent_mocked.py`, `test_controller.py`.
**Risk:** medium
**Deps:** none
**Effort:** L

### T-3-6 — Split `builder.py` → runtime / backends_factory / builtin_tools
**Problem:** `agent/builder.py` 687 lines; `build_runtime` ~180 lines; `_add_wiki_tools` ~120 lines; stale comment re untrusted wiki access.
**Files:** new `src/jarn/agent/{runtime,backends_factory,builtin_tools}.py`; slim `builder.py`.
**Action:**
1. `runtime.py` → orchestrator (`build_runtime`).
2. `backends_factory.py` → `_make_local/docker/langsmith`.
3. `builtin_tools.py` → wiki, repo_map, plan, memory, background wiring.
4. Fix the stale comment (T-1-6 ensures untrusted wiki is actually gated) or align behavior.
**DoD:**
- [ ] All builder-related tests pass; public `build_runtime` API unchanged.
- [ ] No file > ~300 lines.
**Tests:** existing `test_agent_mocked.py`, `test_controller.py`.
**Risk:** medium
**Deps:** T-3-5
**Effort:** M

### T-3-7 — Extract shared `process_util`
**Problem:** `_kill`/`terminate_process_group` duplicated identically in `local_backend.py` and `docker_backend.py`; `background.py` has similar logic.
**Files:** new `src/jarn/agent/process_util.py`; `local_backend.py`, `docker_backend.py`, `background.py`.
**Action:**
1. One `terminate_process_group(pid, grace_secs)` used everywhere.
**DoD:**
- [ ] Backend tests pass; no duplicated kill logic.
**Tests:** existing backend tests.
**Risk:** low
**Effort:** S

### T-3-8 — Extract shared onboarding helpers
**Problem:** `_provider_hint` duplicated in `wizard.py`, `tui_wizard.py`, auth helpers in `repl.py`; module docstrings claim a permission-mode step that doesn't exist.
**Files:** new `src/jarn/onboarding/providers.py`; `onboarding/wizard.py`, `onboarding/tui_wizard.py`, `src/jarn/repl/auth_errors.py`.
**Action:**
1. Move `_provider_hint` (and any other shared helper) to `providers.py`.
2. Either add a permission-mode step to the wizard **or** fix the docstring to match implementation (recommended: fix docstring; mode is a runtime concern).
**DoD:**
- [ ] One source of `_provider_hint`; wizard tests pass.
- [ ] Docstring matches behavior.
**Tests:** existing wizard tests.
**Risk:** low
**Effort:** S

### T-3-9 — Unify YAML store helpers
**Problem:** atomic-write logic duplicated in `settings.py` and `rule_store.py` with divergent corruption handling (fixed in T-1-4 but still two copies).
**Files:** new `src/jarn/config/yaml_store.py`; `settings.py`, `rule_store.py`.
**Action:**
1. One `atomic_write_yaml(path, data, *, backup=True)` + one `safe_load_yaml(path)` that returns `Corrupt|Missing|Ok` (used by T-1-4's fail-closed logic).
**DoD:**
- [ ] Both stores use the shared helpers; corruption tests still pass.
**Tests:** existing + T-1-4 tests.
**Risk:** low
**Deps:** T-1-4
**Effort:** S

### T-3-10 — Migrate `schema.py` to Pydantic + add `config_version` + migration
**Problem:** manual dataclasses + split validation; unknown keys caught only at top level; no versioning/migration.
**Files:** `src/jarn/config/schema.py`, `loader.py`, `tests/test_config.py`
**Action:**
1. Introduce Pydantic models mirroring the dataclasses (with `extra="forbid"` where appropriate to catch nested junk).
2. Add `config_version: int`; write a `migrate(old_config) -> new_config` shim for one prior version (and a hook for future ones).
3. Keep the public `Config` dataclass shape for one release to avoid a breaking import change; convert at the boundary.
**DoD:**
- [ ] Unknown nested keys are rejected with a clear error.
- [ ] An old `config.yaml` (version N-1) loads via the migrator.
- [ ] `test_config` passes + new tests for nested validation + migration.
**Tests:** `test_config.test_nested_unknown_rejected`, `test_config.test_migration_from_prev_version`.
**Risk:** high (touching config core); do behind a feature flag / gradual rollout. Mitigate by keeping the dataclass shim.
**Deps:** T-1-4, T-1-11
**Effort:** L (can defer to post-v1 if risk is too high)

### T-3-11 — keyfix/keys parity for the prompt_toolkit REPL
**Problem:** `tui/keyfix.py` patches Textual's `linux_driver` only; the main REPL uses prompt_toolkit, so the Caps Lock stray-char fix likely doesn't apply to the primary chat UI. `keys.py` inspector is Textual-only.
**Files:** `src/jarn/tui/keyfix.py`, `src/jarn/tui/keys.py`, `src/jarn/repl/keys.py`, docs
**Action:**
1. Add a prompt_toolkit key processor fix (or document why it's unneeded for prompt_toolkit).
2. Add `jarn keys --repl` that logs raw prompt_toolkit keys for debugging.
3. Document `JARN_KEEP_KITTY_ALL_KEYS=1` in README troubleshooting.
**DoD:**
- [ ] Either the fix applies to the REPL (verified on a Caps Lock repro) or the limitation is documented with a workaround.
**Tests:** `test_keyfix` extended if applicable.
**Risk:** low
**Effort:** M

### T-3-12 — Unify theme systems (palette vs theme.py)
**Problem:** REPL uses `palette.configure_ui`; onboarding/keys use Textual `theme.py` — two parallel theme systems.
**Files:** `src/jarn/tui/palette.py`, `src/jarn/tui/theme.py`, docs
**Action:**
1. Generate Textual themes from `_PALETTES` (or document the split clearly).
2. Remove the test-only `apply_ui_theme()` alias (T-4 cleanup) or merge into `configure_ui`.
**DoD:**
- [ ] One palette definition drives both renderers (or the split is documented and intentional).
**Tests:** existing `test_splash`, `test_palette`/`test_settings` theme tests.
**Risk:** low
**Effort:** M

### Phase 3 — Definition of Done
- [ ] `repl.py`, `controller.py`, `session.py`, `builder.py` are packages; no file in them exceeds its target line cap.
- [ ] One slash-command registry drives dispatch + `/help`; no `controller`→`cli` import; doctor render is single-source.
- [ ] Shared `process_util`, `yaml_store`, onboarding helpers exist; no duplicated kill/YAML/_provider_hint logic.
- [ ] Schema is Pydantic with `config_version` + a migrator (or explicitly deferred with a recorded decision).
- [ ] keyfix/keys cover the prompt_toolkit REPL (or documented limitation).
- [ ] Full `pytest -q`, `ruff`, `mypy` green; **no behavior change** vs. Phase 2 end state.

---

## Phase 4 — Features & Cleanup

**Goal:** deliver the missing product features the user asked for ("overall" improvement) and remove all dead code/deps/drift in the same sweep.

### Features (add)

### T-4-1 — Headless multi-turn + `--resume` + structured errors + exit codes
**Problem:** headless is single-turn; no `--resume` for CI; errors are unstructured; `tool_calls` missing from JSON (deferred full impl from T-2-2 if that task removed the flag).
**Files:** `src/jarn/headless.py`, `src/jarn/cli.py`, `docs/CONFIGURATION.md`, `tests/test_headless.py`
**Action:**
1. Multi-turn loop bounded by `--max-turns`; `--resume <session-id>` (or `--resume last`) resumes a prior thread.
2. Structured `--json` error object; exit codes per T-2-2.
3. Budget hard-stop surfaced as exit 2 + a JSON field.
**DoD:**
- [ ] `jarn -p "do X" --max-turns 5` runs up to 5 turns; `--resume last` continues a prior session; `--json` includes `tool_calls` + structured errors.
- [ ] Tests cover multi-turn cap, resume, exit codes, budget stop.
**Tests:** `test_headless.test_multi_turn`, `test_headless.test_resume`, `test_headless.test_budget_stop_exit_code`.
**Risk:** unattended multi-turn amplifies cost/safety; keep fail-closed approver + budget.
**Deps:** T-2-2
**Effort:** M

### T-4-2 — Image paste on Linux + Windows + format fallbacks
**Problem:** `tui/clipboard.py` is macOS PNG-only; Linux/Wayland/X11 and Windows unsupported; JPEG screenshots fail silently.
**Files:** `src/jarn/tui/clipboard.py`, `tests/test_image_paste.py`, docs
**Action:**
1. Linux: `wl-paste` (Wayland) + `xclip -selection clipboard -t image/png` (X11).
2. Windows: PowerShell `System.Windows.Forms.Clipboard.GetImage`.
3. macOS: add TIFF/JPEG fallback after PNG.
4. Cap image size (e.g. 10MB) with a user message.
5. Document supported platforms in README.
**DoD:**
- [ ] Each platform path is tested (mocked subprocess/PowerShell); fallback formats covered.
- [ ] Oversized image is rejected with a message.
**Tests:** `test_image_paste.test_linux_wl_paste`, `test_image_paste.test_windows_powershell`, `test_image_paste.test_macos_jpeg_fallback`, `test_image_paste.test_size_cap`.
**Risk:** low
**Effort:** M

### T-4-3 — Arg-aware slash-command completion
**Problem:** `/command` completes only when the whole line has no space; `/model anth` won't complete model IDs.
**Files:** `src/jarn/tui/completion.py`, `src/jarn/repl/completer.py`, `tests/test_completion.py`
**Action:**
1. Per-command arg completers for `/model` (model IDs), `/mode` (modes), `/preset` (preset names), `/resume`/`/sessions` (session titles), `/mcp` (server names).
2. Keep the no-space fallback for commands without an arg completer.
**DoD:**
- [ ] `/model <tab>` lists model IDs; `/preset <tab>` lists presets; tests cover each.
**Tests:** `test_completion.test_model_arg`, `test_completion.test_preset_arg`.
**Risk:** low
**Effort:** S

### T-4-4 — Per-turn date re-injection
**Problem:** `prompts.date_context()` runs at build time; long sessions drift from "today".
**Files:** `src/jarn/agent/prompts.py`, `src/jarn/agent/session.py`, `tests/test_harness_prompt_ab.py`
**Action:**
1. Re-inject the current local date as a system message at the start of each turn (or when the date rolls over).
**DoD:**
- [ ] Test: a simulated multi-day session has the correct date in each turn's context.
**Tests:** `test_harness_prompt_ab.test_date_per_turn`.
**Risk:** low
**Effort:** S

### T-4-5 — Optional verify gate (post-edit auto-run detected test command)
**Problem:** verification is honor-system; `verify.py` only injects hints.
**Files:** `src/jarn/agent/verify.py`, `src/jarn/agent/session.py`, `src/jarn/config/schema.py`, `tests/test_verify.py`
**Action:**
1. Add `verify.gate: "off" | "suggest" | "auto"` (default `suggest`).
2. `suggest`: after edits, emit a NOTICE with the detected test command and ask the user.
3. `auto`: run the detected command in the sandbox/backend and include pass/fail in the turn result (respecting permissions + danger-guard).
**DoD:**
- [ ] `suggest` mode surfaces the command; `auto` mode runs it and reports results; both tested with mocked commands.
**Tests:** `test_verify.test_gate_suggest`, `test_verify.test_gate_auto_runs_detected_command`.
**Risk:** `auto` could run unexpected commands; keep it gated by permissions + guard + an explicit config opt-in.
**Deps:** T-2-11
**Effort:** M

### T-4-6 — Per-MCP-server timeout + `/mcp status` re-check
**Problem:** no per-server timeout; `/mcp status` reads cached maps from first `ensure_runtime` with no reload.
**Files:** `src/jarn/extensibility/mcp.py`, `src/jarn/config/schema.py`, `src/jarn/tui/controller/commands/diagnostics.py`, `tests/test_extensibility.py`
**Action:**
1. `MCPServer.timeout_secs` config; honored at `get_tools`.
2. `/mcp status` adds a `--refresh` (or `!`) that re-runs health checks.
**DoD:**
- [ ] Test: a slow server times out; `/mcp status --refresh` re-probes.
**Tests:** `test_extensibility.test_mcp_timeout`, `test_extensibility.test_mcp_status_refresh`.
**Risk:** low
**Effort:** S

### T-4-7 — `/telemetry status` command
**Problem:** no user-facing way to audit what telemetry is stored.
**Files:** `src/jarn/observability/telemetry.py`, `src/jarn/tui/controller/commands/diagnostics.py`, `tests/test_telemetry.py`
**Action:**
1. `/telemetry status` shows enabled/disabled, file path, size, event count, install-id presence.
**DoD:**
- [ ] Command output includes all of the above; tested.
**Tests:** `test_telemetry.test_status_command`.
**Risk:** low
**Effort:** S

### T-4-8 — OpenRouter pricing fetch opt-out
**Problem:** `cost/pricing.py` always attempts a startup network fetch; no opt-out for privacy-conscious/offline users.
**Files:** `src/jarn/cost/pricing.py`, `src/jarn/config/schema.py`, `src/jarn/repl/app.py`, `tests/test_cost.py`
**Action:**
1. `pricing.network: false` config (and `JARN_NO_NETWORK_PRICING=1` env) skips the fetch; falls back to bundled prices + user overrides.
2. Surface a one-line notice when network pricing is disabled.
**DoD:**
- [ ] Test: with the flag set, no outbound request is attempted; prices still resolve from bundled/override data.
**Tests:** `test_cost.test_pricing_network_opt_out`.
**Risk:** low
**Effort:** S

### T-4-9 — Doc drift sweep (test counts + ROADMAP + SECURITY + CHANGELOG)
**Problem:** 1166 vs 1183 across ~10 files; ROADMAP contradictions (Docker/sandbox, "unreleased" 0.3.0 header); SECURITY.md lists only 0.1.x; CHANGELOG `[Unreleased]` empty.
**Files:** `README.md`, `README-TH.md`, `JARN.md`, `SPEC.md`, `RELEASE.md`, `docs/README.md`, `docs/CONTRIBUTING.md`, `docs/ROADMAP.md`, `CHANGELOG.md`, `SECURITY.md`, `scripts/swebench_modal.py`
**Action:**
1. Update test count to the current collection total everywhere (and keep T-2-16 enforcing it).
2. Fix ROADMAP: rename stale section headers, reconcile Docker/sandbox checkboxes, rewrite Known limitations to reflect shipped Docker + OS sandbox (host is default, sandbox opt-in).
3. Update SECURITY.md supported-versions table to include 0.4.x.
4. Start `CHANGELOG [Unreleased]` with the post-0.4.4 changes.
5. Fix `swebench_modal.py` hardcoded 0.3.0 wheel (parameterize from `pyproject.toml` version).
**DoD:**
- [ ] T-2-16 doc-sync test passes.
- [ ] ROADMAP has no internal contradictions (reviewer checklist).
- [ ] SECURITY.md table current; CHANGELOG `[Unreleased]` populated.
**Tests:** T-2-16.
**Risk:** low
**Effort:** S

### T-4-10 — OTel exporter path (optional)
**Problem:** tracing is LangSmith-only; users expecting OpenTelemetry have no path.
**Files:** `src/jarn/observability/tracing.py`, `src/jarn/config/schema.py`, docs
**Action:**
1. Add an optional OTel exporter (via `opentelemetry-sdk`) behind `observability.tracing.backend: "langsmith" | "otel"` (default langsmith for back-compat).
2. Document redaction/sampling knobs.
**DoD:**
- [ ] OTel backend configurable; spans export to a configurable endpoint; tested with an in-memory exporter.
**Tests:** `test_observability.test_otel_backend`.
**Risk:** new optional dep; keep it in an `otel` extra.
**Effort:** M

### Cleanup (remove)

### T-4-11 — Remove `pytest-textual-snapshot` dependency
**Problem:** declared in dev deps, zero usage; SPEC says "snapshot retired".
**Files:** `pyproject.toml`, `uv.lock`, `.gitignore` comment (snapshot baseline), `tests/__snapshots__/` if any
**Action:**
1. Remove the dep; `uv lock`; remove the stale `.gitignore` comment referencing `tests/__snapshots__`.
**DoD:**
- [ ] `uv sync --extra dev` succeeds; no test references the dep; lock updated.
**Tests:** none
**Risk:** low
**Effort:** S

### T-4-12 — Remove small dead code
**Problem:** `memory/store.py` `_: None` field; `agent/builder.py` `JarnRuntime.warnings` always `()`; `tui/logo.py` unused `model`/`mode` params; `tui/palette.py` test-only `apply_ui_theme()` alias; `config/defaults.py` `DANGEROUS_COMMAND_HINTS` duplicate of `guard.py`.
**Files:** as listed
**Action:**
1. Remove each dead item; for `DANGEROUS_COMMAND_HINTS` either import from `guard.py` or generate from the rules list; for `JarnRuntime.warnings` either wire it (sandbox degrade, repomap failure) or remove.
**DoD:**
- [ ] Grep confirms no remaining references; tests pass.
**Tests:** existing.
**Risk:** low
**Effort:** S

### T-4-13 — Resolve `skill_dirs()` duplicate / `ProviderEmbedder` unwired
**Problem:** `skill_dirs()` contradicts `load_skills()` order (fixed in T-2-10, here we delete the leftover); `memory/vector.py` `ProviderEmbedder` is documented but never wired.
**Files:** `src/jarn/extensibility/skills.py`, `src/jarn/memory/vector.py`, `tests/test_extensibility.py`, `tests/test_vector.py`
**Action:**
1. Delete `skill_dirs()` (or make it a thin delegating alias) — completes T-2-10.
2. Either wire `ProviderEmbedder` from `memory.embedder` config (provider + model) **or** move it to an `experimental/` module and fix the docstring.
**DoD:**
- [ ] No contradictory duplicate; `ProviderEmbedder` either works via config or is clearly marked experimental/unwired.
**Tests:** `test_vector.test_provider_embedder_wired` (if wired) or a docstring assertion.
**Risk:** low
**Effort:** S

### T-4-14 — Decide on `swebench_modal.py`
**Problem:** one-off research script; stale 0.3.0 wheel; undeclared `modal` dep; no tests.
**Files:** `scripts/swebench_modal.py`, `pyproject.toml` (optional `bench` extra), `docs/CONTRIBUTING.md`
**Action:**
1. **If maintained:** pin `modal` in a `bench` extra, parameterize the wheel version, add a minimal smoke test.
2. **If not:** move to `contrib/` with a README, or delete.
**DoD:**
- [ ] Decision recorded; script either has declared deps + test, or is moved/removed.
**Tests:** smoke if kept.
**Risk:** low
**Effort:** S

### T-4-15 — Deprecation timeline for `policy.profile` / `--profile` / `/profile`
**Problem:** deprecated alias of `/preset` still functional with no removal date.
**Files:** `src/jarn/cli.py`, `src/jarn/config/settings.py`, `src/jarn/tui/controller/commands/config.py`, `CHANGELOG.md`, docs
**Action:**
1. Announce a removal version (e.g. 0.6.0) in the deprecation warning + CHANGELOG + docs.
2. Keep working until then.
**DoD:**
- [ ] Deprecation warning names the removal version; docs + CHANGELOG updated.
**Tests:** existing `test_preset_unify` / `test_profiles`.
**Risk:** low
**Effort:** S

### T-4-16 — Delete local artifacts (workspace hygiene)
**Problem:** `.coverage`, `snapshot_report.html`, `PROJECT_AUDIT_*.md`, stale `dist/jarn-0.{1,2,3}.0-*` wheels, `build/pyinstaller/` exist on disk (gitignored, not tracked).
**Files:** local only
**Action:**
1. Delete the listed local artifacts to clean the working tree.
**DoD:**
- [ ] `ls` confirms removal; `git status` unaffected (they were untracked).
**Tests:** none
**Risk:** none (gitignored)
**Effort:** S

### T-4-17 — Trim `todo.md` completed sections
**Problem:** completed M1–M4 + competitive-gaps blocks add noise.
**Files:** `todo.md` (gitignored, local)
**Action:**
1. Archive completed sections; keep only open "Road to 1.0.0" items.
**DoD:**
- [ ] `todo.md` reflects only open work.
**Tests:** none
**Risk:** none
**Effort:** S

### Phase 4 — Definition of Done
- [ ] Headless multi-turn + `--resume` + structured errors shipped.
- [ ] Image paste works on Linux/Windows + format fallbacks + size cap.
- [ ] Arg-aware completion, per-turn date, verify gate, MCP timeout/refresh, `/telemetry status`, pricing opt-out, OTel path all shipped and tested.
- [ ] All doc drift fixed; doc-sync test green; ROADMAP internally consistent.
- [ ] Dead deps/code removed; `swebench_modal.py` decision recorded; deprecation timeline set; local artifacts cleaned; `todo.md` trimmed.
- [ ] Full `pytest -q`, `ruff`, `mypy` green.

---

## Cross-cutting — Testing & verification strategy

- **Per-task:** every task ships a failing test that passes after the fix (TDD where applicable), runs the targeted test file, then the full suite.
- **Regression guards:** `test_guard`, `test_permissions`, `test_trust`, `test_secrets`, `test_transcript`, `test_checkpoint`, `test_cost`, `test_repl`, `test_controller`, `test_agent_mocked` are the load-bearing suites — run them on every PR.
- **No new skips without a ticket:** any new `skip`/`xfail` must reference an issue.
- **Coverage ratchet:** after T-2-14, coverage floor only moves up.
- **Behavior-preserving refactors:** Phase 3 commits must show no diff in the load-bearing suite outputs (use `pytest --snapshot-update` only if a snapshot legitimately changes, with justification).

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Danger-guard over-broad patterns flag benign commands | med | med | Use DANGEROUS (not BLOCKED) for installers; document; allowlist in `permissions.allow` |
| Pydantic migration breaks existing configs | low | high | Keep dataclass shim one release; migrator + `config_version`; feature-flag |
| Phase 3 splits introduce import cycles / subtle behavior change | med | med | One-file-per-commit; load-bearing suite between every move; no logic edits in move commits |
| Headless multi-turn amplifies cost/safety in unattended use | med | high | Fail-closed approver; budget hard-stop; danger-guard; document |
| Removing `pytest-textual-snapshot`/`skill_dirs` etc. breaks a hidden consumer | low | low | Grep + full suite before removal |
| Windows CI surfaces platform-only failures | med | low | Allow-failure initially; document WSL-only features |
| Hook env allowlist breaks user hooks relying on inherited env | med | med | `hooks.inherit_env: true` escape hatch + migration note |

---

## Explicit non-goals (this plan)

- **Sandbox-by-default for everyone.** Untrusted repos are secondary; we harden the guard + trust + hook surface instead of forcing docker/bwrap on all users.
- **A real shell AST/lexer in the danger-guard.** It's a net, not a sandbox; we expand patterns + document limits (T-1-1) rather than build a parser.
- **Web UI.** Roadmap says post-launch; `web/` is a sketch.
- **Live-LLM tests in PR CI.** Cost/slow; nightly-only (T-2-18).
- **Rewriting the provider layer.** We restrict `extra` (T-1-11) and fix caching (T-2-7) but don't re-architect providers.
- **Removing the `!` shell escape.** Intentional host escape hatch; we only add a reminder (T-1-1).

---

## Suggested sequencing (summary)

1. **Phase 0** (half day) → **Phase 1** (~2 weeks) → **Phase 2** (~2 weeks) are sequential and highest-value.
2. **Phase 3** can start once Phase 1's security tests are green (refactors don't touch the new security logic); allow ~3 weeks.
3. **Phase 4** features can ship in parallel with Phase 3 once their target files are stable; cleanup lands last.
4. Tag releases at the end of each phase (e.g. `0.4.5` security, `0.4.6` bugs+release-safety, `0.5.0` refactor, `0.6.0` features+cleanup) and update the deprecation timeline accordingly.

---

## Appendix — Finding inventory (source reviews)

Findings were synthesized from five parallel subsystem reviews:
- `src/jarn/agent/` — backend duplication, checkpoint edge cases, background lifecycle, verify/repomap gaps, session/builder split.
- CLI/REPL/TUI/onboarding — `repl.py`/`controller.py` splits, dead `/clear` flag, `--max-turns`, doctor duplication, keyfix/clipboard parity.
- config/providers/permissions — untrusted sanitization gaps, danger-guard bypasses, inline secrets, YAML wipe, scope CWD bug, trust TOCTOU.
- memory/cost/observability/extensibility — hook shell/env, transcript+log redaction gaps, doctor skill precedence, unbounded memory injection, streaming usage double-count, `ProviderEmbedder` unwired.
- tests/docs/packaging/scripts — test-count drift, release-without-gates, ROADMAP contradictions, unused `pytest-textual-snapshot`, no coverage gate, repo hygiene (clean).

Each finding above maps to one or more tasks; tasks with no originating finding are structural glue (Phase 0, Phase 3 splits, Phase 4 cleanup consolidation).
