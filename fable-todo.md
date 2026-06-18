# fable-todo.md — JARN customer-feedback remediation

> **✅ STATUS: COMPLETE (2026-06-18)** — ทุก task P1.A–P5.D ลงครบแล้ว
> (`68782db` P1 · `fe25a91` P2 · `02d40de` P3-small · `c46bac6` P3.A/UNIFY · `0cc3db1`+`67532d0` P4 · `fb2b66c` P5).
> ไฟล์นี้เก็บไว้เป็น record ของรอบนี้ — แผน "ทำต่อ" อยู่ที่ `todo.md` (§ Road to 1.0.0).

**For:** Fable 5 (implementer)
**Author:** triage + plan from a 25-item customer feedback round, verified against the codebase (every claim checked with file:line evidence).
**Scope:** Implement every task below. Tasks are grouped into 5 phases ordered by effort/impact. Each task is self-contained: problem → files → approach → acceptance criteria → risk.

## Ground rules

- **Verify before you change.** Each task cites `file:line` anchors that were accurate at planning time; they may have drifted — re-grep to confirm before editing.
- **Tests are mandatory.** This repo ships ~789 tests (`tests/`). Every behavior change needs a new/updated test. Run `uv run pytest` (or the project's runner) and keep it green. Run `ruff` + `mypy` (caches exist: `.ruff_cache`, `.mypy_cache`).
- **Match surrounding style.** Dataclasses for config, `CommandResult` returns for REPL commands, Rich markup with `palette.C_*` constants for output.
- **Back-compat.** Config keys and CLI flags are a public surface. Never hard-break: deprecate with a shim + one-time warning.
- **Don't touch the 3 non-issues** (see "Out of scope" below). They were verified as already-handled.

## Decision log (locked by product owner)

1. **Autocheckpoint stays `False` by default.** Do NOT flip `git.autocheckpoint`. Instead improve the no-checkpoint UX + discoverability (task **P3.B**).
2. **"yolo" keeps its name.** Do NOT rename the mode. Add an explicit warning/confirmation on entry instead (task **P3.C**).
3. **Unify `permission_mode` + `policy.profile` into ONE system.** This is a real refactor, not a docs fix. Full design spec in task **P3.A / UNIFY**.

## Out of scope — verified NOT real issues (do not implement)

| Claim | Why skipped |
|---|---|
| #6 "/help doesn't list Ctrl+O" | Already listed — `HELP_SHORTCUTS` in `extensibility/commands.py:132-136` + splash `repl.py:184`. |
| #14 "no collapse/expand of tool output" | Ctrl+O pager already exists — `repl.py:212` (hint), `repl.py:485` (`_open_pager`). Output is summarized in scrollback, not flooded. Premise is inverted. |
| #24 "! shell escape is silent" | Already prints `⚡ host shell — runs on your machine directly; no agent, no approval` in red — `repl.py:859-862`. |

Note: #6/#24 are *discoverability* gaps for features that exist — partially addressed by the better `/help` grouping in **P1.D**. No dedicated work.

---

# Phase 1 — Quick wins (low risk, high visibility)

## P1.A — Suppressible / compact splash  (claim #21, verdict TRUE)
**Problem:** Full ASCII splash prints unconditionally every session; no way to disable or show-once. `repl.py:180` calls `splash()` always; `tui/logo.py` has no compact variant; no config flag.
**Files:** `config/schema.py` (`UIConfig`, ~line 221), `config/defaults.py`, `tui/logo.py`, `repl.py:180-185`.
**Approach:**
- Add `UIConfig.splash: str = "compact"` with values `full | compact | off`.
- Add `splash_compact(version, model, ...)` to `logo.py`: one-line wordmark + version + the existing one-line shortcut hint.
- In `repl.py`, branch on `config.ui.splash`. Special case: show `full` automatically on genuine first run (no `~/.jarn` state yet), then fall back to the configured value. Track first-run via existence of a state marker under `paths.global_home()`.
**Acceptance:**
- `ui.splash: off` → no splash banner (still print the one-line shortcut hint so Ctrl+O etc. stays discoverable).
- `ui.splash: compact` (default) → single-line banner.
- First-ever run shows `full` once regardless of setting.
- Test in `tests/` covering all three values + first-run path.
**Risk:** Low.

## P1.B — `/doctor` inside the REPL  (claim #22, verdict TRUE)
**Problem:** `jarn doctor` is CLI-only (`cli.py:91`, impl `_cmd_doctor` ~`cli.py:263/368`); no `/doctor` slash command. `extensibility/commands.py:BUILTINS` has no doctor entry.
**Files:** `cli.py` (extract diagnostic core), `doctor_extensions.py` (module already exists — good home), `extensibility/commands.py:BUILTINS`, `tui/controller.py` (dispatch).
**Approach:**
- Refactor the doctor diagnostic body out of the CLI command into a reusable function returning structured results (so both `jarn doctor` and `/doctor` render the same data). The CLI path already builds a `diag` dict (`cli.py:289-298`) — lift that.
- Register `doctor` in `BUILTINS` and dispatch to a `_cmd_doctor` in the controller that renders the diagnostic inline (Rich), including provider key validation + current trust + effective mode.
**Acceptance:** `/doctor` in a live session prints the same checks as `jarn doctor`; no need to exit. Test the controller command.
**Risk:** Low–medium (shared refactor; keep CLI output identical).

## P1.C — Actionable key-validation hint  (claim #25, verdict TRUE)
**Problem:** Status bar shows `✗` for a failed provider key with no fix pointer. `tui/controller.py:213-216` renders the glyph; `last_error` (`controller.py:86`) is printed once (`repl.py:245-253`) but never linked.
**Files:** `tui/controller.py:213-216`, `repl.py:245-253`, `tui/toolbar.py`.
**Approach:**
- When `health == "error"`, render the status as `✗ key · /doctor` (or append `run /doctor` to the one-time startup error notice).
- Ensure the pointer names a real next step now that `/doctor` exists (depends on **P1.B**).
**Acceptance:** A bad/missing key surfaces a visible, actionable pointer to `/doctor` (and/or `/config`). Test the status-render branch.
**Risk:** Low. **Do P1.B first.**

## P1.D — Grouped `/help` + glyph legend  (claims #7, #8, #23, partial)
**Problem:** `/help` is a flat list (descriptions exist in the palette `completion.py:59`, but `format_help` `commands.py:169-196` doesn't group). Toolbar glyphs (`◇◆⚡⚠`, `●/✗`, `queue N`) have no legend.
**Files:** `extensibility/commands.py` (`BUILTINS` add a `group` field; `format_help`), `tui/palette.py` (reuse `MODE_GLYPH`).
**Approach:**
- Add a `group` to each builtin: **Daily** (model, mode, cost, undo, redo, compact, memory, clear), **Setup** (config, profile→see P3.A, sandbox, trust, mcp, skills, init), **Session** (resume, sessions, checkpoints, queue, map, wiki), and a **Shortcuts** block (already exists) + a new **Toolbar glyphs** legend block: `◇ plan · ◆ ask · ⚡ auto-edit · ⚠ yolo · ● key ok · ✗ key fail · queue N = lines waiting while a turn runs`.
- `format_help` renders by group with section headers.
**Acceptance:** `/help` shows grouped commands with descriptions + a glyph legend. Snapshot/format test updated.
**Risk:** Low. **Coordinate the `profile` entry with P3.A** (it becomes `preset` or is removed).

---

# Phase 2 — Onboarding & providers

## P2.A — Beginner-friendly wizard  (claim #1, verdict PARTIAL)
**Problem:** Plain wizard lists 13 providers with no hint and no "recommended"; never detects an existing key in env. `wizard.py:122-124` (default openrouter), `wizard.py:165-184` (`_configure_key` doesn't probe env); TUI hints exist only in `tui_wizard.py:50-58`.
**Files:** `onboarding/wizard.py`, `onboarding/tui_wizard.py`.
**Approach:**
- **Env detection first:** probe `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` (and the obvious others). If found, lead with `✓ Found <PROVIDER>_API_KEY in your environment — use it? [Y/n]` and pre-select that provider.
- **Recommendation tag:** mark the recommended provider with `★ recommended` — the one whose key is in env, else Anthropic if `ANTHROPIC_API_KEY` present, else OpenRouter (current default) with a one-liner "aggregator; needs a free openrouter.ai account".
- **Parity:** port the `(cloud)/(local)/(custom)` hints from the TUI wizard into the plain wizard.
**Acceptance:** With `ANTHROPIC_API_KEY` set, the wizard offers it up-front and tags Anthropic recommended; plain + TUI wizards show provider-class hints. Tests for env-present and env-absent paths.
**Risk:** Low–medium (don't store the env key verbatim into config — keep the `${ENV}` reference form, matching existing secret handling).

## P2.B — Model-slug "did you mean" + unpriced warning  (claim #2, verdict TRUE)
**Problem:** Dot-vs-dash slug confusion (`anthropic/claude-opus-4.8` OR vs `claude-opus-4-8` Anthropic). No normalization or suggestion; unresolved → generic error (`providers/models.py:192-198`); pricing miss is silent "unpriced" (`cost/pricing.py:19,223-234`).
**Files:** `providers/models.py`, `cost/pricing.py`.
**Approach:**
- Add `suggest_slug(provider_type, slug)`: on resolution failure, try the dot↔dash swap and compare against the provider's known/default slugs; if a near-match exists, raise/augment the error: `Model 'claude-opus-4.8' not found for provider 'anthropic' — did you mean 'claude-opus-4-8'? (Anthropic uses dashes; OpenRouter uses dots.)`
- On a **pricing** miss, emit a one-time per-model warning to the user: `⚠ No price for <slug> — cost will be counted as $0`. (Don't spam; dedupe per model id.)
**Acceptance:** A dot/dash-swapped slug yields a "did you mean" message naming the correct form. An unpriced model logs the visible warning once. Unit tests for both, plus the namespace note (see `defaults.py:9-14`).
**Risk:** Low.

## P2.C — Local model discovery (Ollama / LM Studio)  (claim #20, verdict TRUE)
**Problem:** Local providers require typing the model name blind; no endpoint listing (`models.py:168-181`, `tui_wizard.py:198-204`). No call to ollama `/api/tags` or LM Studio/OpenAI-compatible `/v1/models`.
**Files:** `providers/models.py` (new `list_remote_models`), `onboarding/tui_wizard.py:198-204`, REPL `/model` (`tui/controller.py:590`).
**Approach:**
- Add `list_remote_models(provider) -> list[str]`: ollama → `GET {base_url}/api/tags` (parse `.models[].name`); lmstudio / openai_compatible → `GET {base_url}/v1/models` (parse `.data[].id`). Short timeout; on failure return `[]` (fall back to manual entry — never block).
- Wizard: if the list is non-empty, present an **arrow-key selection** (the user prefers arrow-select over typing in TUIs) instead of free text.
- Add `/model refresh` (or `/model list`) in the REPL to re-query and pick.
**Acceptance:** With a reachable Ollama/LM Studio endpoint, the wizard and `/model` offer a selectable list; unreachable endpoint degrades to manual entry with a note. Tests mock the HTTP responses.
**Risk:** Medium (network/timeout handling; must fail open to manual entry).

---

# Phase 3 — Permissions, trust & approval-safety

## P3.A / UNIFY — Collapse `permission_mode` + `policy.profile` into one model  (claim #3) ★ highest-risk task

### Why
Today there are two parallel user-facing axes:
- `permission_mode` ∈ {plan, ask, auto-edit, yolo} — the approval-coarseness knob the engine consumes (`schema.py:18-31`, `permissions/engine.py`). Surfaced via `/mode`, Shift+Tab, `--permission-mode`.
- `policy.profile` ∈ {trusted-repo, review-only, sandbox-required, ci, offline} — a **macro** that, when applied, overwrites four knobs at once (`profiles.py:67-82`): `permission_mode` + `execution.local_sandbox` + `execution.sandbox_allow_network` + `policy.web_tools`. Surfaced via `/profile`, `--profile`.

A profile is **pure sugar**: three of its four effects already have a permanent home (`execution.local_sandbox`, `execution.sandbox_allow_network`, `policy.web_tools`); the fourth IS `permission_mode`. So the second "system" is not an independent axis — it's a preset expander. That's the confusion customers report.

### Target model (the one system)
Three orthogonal, non-overlapping concepts, each with exactly one command:
- **Mode** = how much the agent may do without asking. `{plan, ask, auto-edit, yolo}`. Commands: `/mode`, Shift+Tab, `--mode`.
- **Sandbox/execution** = where code runs + network/web. `execution.local_sandbox`, `execution.sandbox_allow_network`, `policy.web_tools`. Command: `/sandbox`.
- **Trust** = the gate/clamp. Command: `/trust`. (Unchanged conceptually.)
- **Presets** = *launch-time shortcuts* that expand to (mode + sandbox knobs) once and print what they set. Not a persistent parallel config axis.

> One-line mental model for docs: **Mode = how much I approve · Sandbox = where code runs · Trust = the gate · Presets = shortcuts that set the first two.**

### Concrete changes
1. **Rename `profiles.py` concept → presets.** Keep the `PROFILES` table (rename to `PRESETS`) and `apply_*` expansion, but reframe in docstrings as "launch-time preset expansion," not a runtime axis.
2. **Deprecate `policy.profile` config key.** Keep the field for back-compat read. On load, if set: expand it as a preset at the launch boundary + emit a one-time deprecation warning (`policy.profile is deprecated; it sets mode=<x>, sandbox=<y>. Set those directly or use --preset.`). Do not write it back.
3. **Untrusted floor expressed directly, not via a profile.** Today `resolve_effective_profile` forces `apply_profile(config, "review-only")` for untrusted projects (`profiles.py:102-104`). Replace with a direct clamp: untrusted ⇒ `permission_mode = min(mode, PLAN)` **and** force the sandbox knobs the floor implies (network/web posture matching today's review-only floor). **Behavior must stay byte-for-byte equivalent** to the current floor — there is already a parallel direct mode-clamp at `controller.py:782-784`; consolidate on it. Keep the clamp at the launch boundary and one-way (`profiles.py:9-14` invariant).
4. **CLI:** add `--mode` (alias of `--permission-mode`, keep old as hidden alias) and `--preset` (alias of `--profile`, keep old as hidden alias + deprecation warning). Update the "`--profile` overrides `--permission-mode`" warning (`cli.py:213-216`) to the preset framing.
5. **REPL:** replace `/profile` (`controller.py:647-668`) with `/preset NAME` that expands + echoes exactly what it set (`preset 'ci' → mode=yolo, sandbox=require, network=on`); `/mode` and `/sandbox` remain the live axes. Update `BUILTINS` + `/help` grouping (coordinate with **P1.D**).
6. **Trust:** keep `policy` and `permission_mode` in `DANGEROUS_TOP_KEYS` (`trust.py:42-51`) — an untrusted project still must not escalate. Re-resolution on `/trust` (`controller.py:716-755`) now re-applies via the new direct path, not `resolve_effective_profile`.
7. **doctor diagnostics:** `cli.py:289-298` surfaces `permission_mode`, `policy_profile`, `effective_profile`. Recast as `mode` + `preset (deprecated)` + `effective mode (after trust clamp)`.
8. **Docs:** rewrite `docs/PERMISSIONS.md` (244 lines) around the single model; move the 5 presets to a short appendix table.

### Invariants to preserve (do not regress)
- Untrusted projects can never be loosened past the floor; clamp runs last, at the launch boundary, one-way.
- Config loading stays pure (no profile/preset application inside `loader.load_config`).
- Existing configs with `policy.profile` and existing `--profile` invocations keep working (shim).

### Acceptance
- A user can fully drive permissions with `/mode` + `/sandbox` + `/trust`; `/preset` is optional sugar that visibly expands.
- Old `policy.profile` config and `--profile` flag still work, emitting a deprecation notice and producing identical effective settings as before.
- Untrusted-floor tests pass unchanged (effective settings identical pre/post refactor) — add an explicit equivalence test.
- `docs/PERMISSIONS.md` reflects the one-system model.
**Risk:** High. Land behind thorough tests; do an equivalence test that asserts the new direct floor == old `apply_profile("review-only")` result for every mode/preset combination before deleting the old path.

## P3.B — No-checkpoint UX (autocheckpoint stays OFF)  (claim #13, decision: keep default False)
**Problem:** `git.autocheckpoint` defaults `False` (`schema.py:242`); `/undo` with no checkpoints returns "nothing to undo" (`checkpoint.py`), so the selling-point feature silently doesn't work until enabled.
**Files:** REPL `/undo` handler, `tui/controller.py`, possibly a first-edit hook.
**Approach (do NOT change the default):**
- When `/undo` (or `/redo`) runs and **autocheckpoint is disabled**, replace "nothing to undo" with an actionable message: `No checkpoints — /undo needs autocheckpoint. Enable it with /config (git.autocheckpoint: true) or 'jarn config'.`
- Optionally: the first time the agent writes a file in a session while autocheckpoint is off, print a one-time hint that `/undo` is unavailable until enabled. Keep it to once per session; gate behind a quiet flag.
**Acceptance:** `/undo` with the feature off explains exactly how to turn it on; with it on, behaves as today. Test both branches.
**Risk:** Low.

## P3.C — Warn on entering "yolo" (keep the name)  (claim #12, decision: keep name + warn)
**Problem:** `yolo` auto-allows everything except danger-guarded actions (`engine.py:165-166`); the name stays, but entering it should be deliberate.
**Files:** `tui/controller.py:598-613` (`_cmd_mode`), `cli.py` (when `--mode yolo`/`--permission-mode yolo`), `docs/PERMISSIONS.md`.
**Approach:**
- On switching **into** yolo via `/mode yolo` (and Shift+Tab landing on yolo), require a one-time confirmation: `yolo = no approval prompts (danger-guard still blocks catastrophic actions). Continue? [y/N]`. Don't re-prompt on every turn; prompt only on the transition.
- Strengthen the docs/toolbar wording (toolbar already shows `⚠` red — keep).
- `--mode yolo` on the CLI: print a clear one-line warning at startup (no interactive prompt in headless).
**Acceptance:** Entering yolo interactively requires confirmation once; leaving and re-entering re-confirms; headless prints a warning. Test the transition guard.
**Risk:** Low.

## P3.D — Persistent trust indicator  (claim #5, verdict TRUE)
**Problem:** No always-visible trust state; discovered only on error (`controller.py:608,626,1115`). Toolbar has no trust segment (`toolbar.py:44-113`).
**Files:** `tui/toolbar.py` (add `trusted` param + segment), `repl.py:393-401` (pass it), `tui/controller.py` (source trust state).
**Approach:**
- Add a high-priority toolbar segment: `🔒 trusted` / `⚠ untrusted`. Untrusted variant carries the pointer `jarn trust`. Mark it high-priority so the narrow-terminal collapse (`toolbar.py:98-108`) keeps it.
- Source from `is_project_trusted` (`trust.py:88`) computed once at launch + refreshed on `/trust`.
**Acceptance:** Trust state is visible in the toolbar at all times; flips live after `jarn trust` / `/trust`. Toolbar test updated.
**Risk:** Low.

---

# Phase 4 — Diff, approval & rollback (sharpest pains)

## P4.A — See the full diff before approving  (claim #4, verdict TRUE) ★ top user pain
**Problem:** Approval diff hard-capped at 40 lines (`_APPROVAL_DIFF_MAX_LINES = 40`, `repl.py:81`), shown as `… (+N more lines)`, with **no way to view the rest before deciding**. The Ctrl+O pager shows tool output, not approval diffs.
**Files:** `repl.py:81` (constant), `repl.py:1217-1227` (approval options), `repl.py:485` (`_open_pager` — reuse), `tui/widgets/diff.py`.
**Approach:**
- Make the cap configurable: `ui.approval_diff_lines: int = 40` in `UIConfig`.
- Add a `[v] view full diff` option to the approval menu that routes the complete diff through the existing pager (`_open_pager`) so the user can scroll it, then returns to the same approve/deny prompt.
**Acceptance:** When a diff exceeds the cap, the approval prompt offers `v` to view the full diff in the pager; choosing it doesn't auto-approve. Cap is configurable. Tests for the >cap path and the view action.
**Risk:** Medium. **Highest user value in this set — prioritize.**

## P4.B — Edit-before-apply (minimum interactive approval)  (claim #15, verdict TRUE)
**Problem:** Approval is binary allow/deny (`repl.py:1217-1227`); no per-hunk, no edit-before-apply.
**Files:** approval flow in `repl.py`, `agent/files.py`, `tui/widgets/diff.py`.
**Approach (scoped to the achievable win):**
- Add an `[e] edit before apply` option: open the proposed new file content (or the diff) in `$EDITOR`; apply the user-edited result instead of the agent's original. Validate the edited content still applies cleanly.
- **Per-hunk approval is explicitly deferred** (needs hunk parsing + partial apply; high effort). Leave a `# TODO(per-hunk)` marker and note it in docs; do NOT attempt it in this pass.
**Acceptance:** The approval menu offers `e`; the edited content is what lands on disk; aborting the editor cancels cleanly. Test the edit-then-apply path. Document that per-hunk is future work.
**Risk:** Medium. **Log the per-hunk deferral so it isn't mistaken for done.**

## P4.C — `/abort` = cancel + roll back the turn  (claim #16, verdict PARTIAL)
**Problem:** Esc cancels (`repl.py:765`, `_cancel_turn`) but leaves edits on disk; no `/abort`, no single stop-and-rollback.
**Files:** `repl.py:749-768`, `extensibility/commands.py:BUILTINS`, `agent/checkpoint.py`.
**Approach:**
- Add `/abort`: cancel the current turn (reuse `_cancel_turn`) **and** restore the working tree to the checkpoint taken at the turn's start.
- This requires a checkpoint to exist → depends on autocheckpoint being on. Since the default stays OFF (decision #1), `/abort` must degrade gracefully: if no checkpoint, cancel-only and tell the user rollback was unavailable (mirror the P3.B wording pointing at autocheckpoint).
**Acceptance:** With autocheckpoint on, `/abort` stops the turn and reverts its edits in one action; with it off, `/abort` cancels and explains rollback needs autocheckpoint. Tests for both.
**Risk:** Medium. **Do after P3.B so the messaging is consistent.**

## P4.D — `/compact` preview + confirm  (claim #18, verdict TRUE)
**Problem:** `/compact` generates the summary and applies it before the user sees it (`controller.py:216-261`, `repl.py:891-905`); no preview, no confirm — possibly-important context vanishes.
**Files:** `tui/controller.py:216-261` (split generate vs apply), `repl.py:891-905`.
**Approach:**
- Split into two steps: (1) generate summary → render it; (2) prompt `Apply this compaction? [y/N/edit]`. Only replace the thread on `y`. `edit` opens the summary in `$EDITOR` before applying.
- Preserve the auto-compact path (`context.auto_compact`, `schema.py:88`) as-is — this preview is for the **manual** `/compact` command only (auto-compact must remain non-interactive).
**Acceptance:** Manual `/compact` shows the summary and waits for confirmation; declining keeps the original context intact; auto-compact behavior unchanged. Tests for accept/decline/edit and that auto-compact is untouched.
**Risk:** Medium.

---

# Phase 5 — Inline detail surfacing & docs

## P5.A — Web-search sources in the inline summary  (claim #17, verdict PARTIAL)
**Problem:** URLs are in the full tool output (`web_tools.py:233`) but the scrollback summary collapses to `N lines` (`repl_renderer.py:220`); sources are only visible via Ctrl+O.
**Files:** `repl_renderer.py` (tool-summary path, ~line 220), `agent/web_tools.py:233`.
**Approach:**
- Give `web_search` a richer one-line summary that names the top source hosts: `🔍 5 results · example.com, wikipedia.org, …`. Parse hosts from the result URLs in the summary builder (or have the tool expose a compact `sources` field the renderer can read).
**Acceptance:** A web search shows source hosts inline without requiring Ctrl+O. Test the summary formatting.
**Risk:** Low.

## P5.B — Memory "what do I know" view  (claim #11, verdict PARTIAL)
**Problem:** `/memory` lists name+desc only; no unified view of everything the agent will actually inject. Fragmented across `store.py`, `context.py:127`, `vector.py`. No dump/summary (`controller.py:944-951`).
**Files:** `tui/controller.py:921-951`, `memory/context.py:127-136`, `memory/store.py`.
**Approach:**
- Add `/memory dump` (a.k.a. `/memory context`): render exactly what gets injected into the system prompt for this project — global MEMORY.md index + project MEMORY.md index + loaded context file (JARN.md/…) + current top-k recall — in one view, with scope labels.
- Keep existing `/memory` CRUD subcommands.
**Acceptance:** `/memory dump` shows a single consolidated "what the agent knows about this project right now" view. Test it assembles all sources.
**Risk:** Low–medium.

## P5.C — Per-tool cost breakdown  (claim #19, verdict PARTIAL) — optional / lower priority
**Problem:** Cost is tracked per-model (`tracker.py:117-122`) but not per-tool/per-step; can't see which tool burned the most.
**Files:** `cost/tracker.py:103-123`, `/cost` command.
**Approach:**
- Extend the tracker to attribute cost per tool name (and optionally per turn). `/cost` gains a "top burners" section.
- This is the lowest-priority item; implement only if Phases 1–4 land cleanly. Per-model already covers the common case.
**Acceptance:** `/cost` shows a per-tool breakdown of top cost contributors. Tests for attribution.
**Risk:** Medium (touches accounting; keep totals reconciling exactly).

## P5.D — Docs: context-file priority + extensibility decision guide  (claims #10, #9)
**Problem:**
- #10 (TRUE): "first present wins" over `["JARN.md","AGENTS.md","CLAUDE.md"]` (`context.py:78-83`, `defaults.py:236`) is undocumented in the main docs and counterintuitive.
- #9 (PARTIAL): 5 extensibility surfaces are each documented in `EXTENDING.md` but there's no "use which, when" table; skills vs custom commands read as overlapping.
**Files:** `docs/CONFIGURATION.md`, `docs/EXTENDING.md`, `memory/context.py` (startup echo).
**Approach:**
- Document the context-file precedence table (`JARN.md > AGENTS.md > CLAUDE.md`, first present wins, configurable via `compat.context_files`). Also print a one-line startup notice naming which context file was loaded.
- Add a decision table to `EXTENDING.md`: Skills (agent-driven knowledge, persistent) · Custom commands (user-invoked one-off prompt template) · Subagents (delegated task graphs) · Hooks (shell on lifecycle events) · MCP (external tool servers) — with a one-line "use when…" for each and an explicit skills-vs-commands contrast.
**Acceptance:** Both docs updated; a session prints which context file it loaded. (Docs-only except the startup echo.)
**Risk:** Low.

---

## Suggested execution order
1. **Phase 1** (P1.A–P1.D) — fast, visible, unblocks P1.B→P1.C dependency.
2. **P4.A** — single highest user-value fix; pull it forward.
3. **Phase 3** — P3.B/P3.C/P3.D (small) first, then **P3.A/UNIFY** (large, gated by full equivalence tests).
4. **Phase 2** (P2.A–P2.C).
5. Remaining **Phase 4** (P4.B–P4.D) and **Phase 5** (P5.A–P5.D); P5.C last / optional.

## Dependencies
- P1.C depends on **P1.B** (needs `/doctor` to point at).
- P4.C depends on **P3.B** (consistent autocheckpoint messaging).
- P1.D's `profile` entry depends on **P3.A** (becomes `preset`).
- P4.A reuses the existing pager (`repl.py:485`) — no new infra.
