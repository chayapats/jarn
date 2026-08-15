# Hermes-aligned display & command standard

- **Status:** implementing (waves A–K plus list/page primitives in tree).
- **Goal:** make every J.A.R.N. command and live surface as easy to scan as
  [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/cli) —
  plain language, one visual grammar, color with meaning, spacing that groups
  information — without copying Hermes branding or kawaii chrome.
- **Surfaces in scope:** interactive REPL, slash-command output, `jarn --help`
  and CLI errors, `jarn doctor`, onboarding splash, Telegram gateway copy.
- **Non-goals:** a second TUI framework, Hermes skins marketplace, voice,
  personalities, pets, or rewriting the permission/engine core.

---

## 1. Why this work exists

J.A.R.N. already has a palette, an adaptive toolbar, grouped `/help`, and a
streaming turn renderer. The *pieces* exist. The *standard* does not.

A new user currently meets:

1. A dense `/help` dump: three groups, every command on one line, usage strings
   like `[/ref|refresh]` and `[search|show|add|update|delete|dump] ...`.
2. Command replies that are each a one-off `CommandResult("\n".join(lines))`
   with mixed `[cyan]`, `[green]`, `[yellow]`, `[dim]`, and palette hex tokens.
3. A tool stream of `⏺ name  k=v  k=v` plus `  ⎿ summary · 1.2s · ctrl+o` —
   glyphs that `/help` has to explain in a legend because they are not obvious.
4. A toolbar of `model | ◆ ask | cwd … | ctx 42% | $0.0123 · 1,204 tok` with
   no fill bar, no session timer, and no always-visible YOLO warning.
5. `jarn --help` as a long argparse wall plus a useful but still-dense epilog.
6. Hardcoded colors in `ConfigPanel` / `ModulePanel` / `format_settings` that
   ignore the active theme.

Hermes is the reference because it solved the same class of problem: **one
grammar for banner, status bar, slash commands, tool progress, and help**,
with quiet defaults and width-adaptive layout. J.A.R.N. should reach that
clarity while keeping its own identity (permission-first, cyan/teal, “just a
reliable nerd”).

---

## 2. What we take from Hermes — and what we do not

### Take (behavior and layout)

| Hermes pattern | Why it works | J.A.R.N. mapping |
|---|---|---|
| Persistent status bar: model · tokens used/max · **fill bar** · cost · duration | Glanceable session health | Upgrade `render_toolbar` |
| Context color: green &lt;50% · yellow 50–80% · orange 80–95% · red ≥95% | Pressure is visible before overflow | Replace the 70/90-only toolbar colors |
| Quiet default tool feed; `/verbose` cycles density | Scrollback stays readable | New `ui.tool_progress` + `/verbose` |
| `/focus` hides tool chrome, keeps the answer | Power users can go dense; everyone else sees the point | New `/focus` |
| Categorized `/help` plus `/help &lt;cmd&gt;` | Discover without drowning | Rewrite `format_help` |
| Case-insensitive slash commands | `/HELP` just works | Normalize in `handle_command` / `parse_input` |
| Skills as slash commands | `/review-pr` not `/skill review-pr` | Register skill names in the catalog |
| Banner that states model, cwd, tools/skills | First frame orients you | Splash info strip under the wordmark |
| `!cmd` is a cost-free shell, not a security bypass | Hermes still runs approvals | Keep J.A.R.N.’s red shell-escape; **do not** weaken the danger-guard. Document the difference clearly: Hermes `!` still goes through approvals; J.A.R.N. `!` is host-direct and must stay visually alarming. |
| Usage errors that name the command, show syntax, and point at `/help &lt;cmd&gt;` | Failed commands teach | Shared `usage_error()` helper |
| Width collapse: full / compact / minimal | Narrow terminals still work | Toolbar already drops segments; extend the same idea to help, status, doctor |
| Markdown-light final replies in the terminal | Prose, not raw `**bold**` soup | Optional strip of wrapper markup; keep fences and lists |
| Session recap on `/status` (local, no LLM) | Answers “where am I?” | Extend `cmd_status` |
| Visual `/context` breakdown | `/cost` today mixes money and injection | Split `/context` from `/cost` (alias `/usage` → `/cost`) |

### Do not take

- Kawaii faces, pet/hatch, personality packs, voice.
- Full YAML skin engine / skin marketplace (`/skin ares`). J.A.R.N. already has
  `ui.theme` + `ui.accent`. A later optional glyph-pack is wave 6+, not required.
- Alternate-screen Ink TUI as a second product. J.A.R.N. stays native-scrollback
  + prompt_toolkit. Overlays we already have (pickers, `/config` panel) stay.
- Cloning Hermes command names where J.A.R.N. already has a better one
  (`/mode` stays; we do not add `/yolo` as the primary spelling).
- Weakening YOLO / danger-guard copy to look friendlier.

---

## 3. Current-state audit (grounded in code)

### 3.1 Color is not one system

Canonical tokens live in `src/jarn/tui/palette.py` (`C_USER`, `C_TOOL`,
`C_NOTICE`, `C_ERROR`, `C_WARN`, `C_SUCCESS`, `C_DIM`, `ACCENT`, cost/ctx
ramps, mode colors). `configure_ui()` applies theme + accent.

Bypasses today:

- `src/jarn/doctor/render.py` — Rich named colors `[green]`, `[red]`,
  `[yellow]` instead of palette tokens. Light/high-contrast themes cannot
  retint doctor.
- `src/jarn/config/settings.py` — `_C_ACCENT = "#22d3ee"` etc. hardcoded;
  `ConfigPanel` ignores `palette.configure_ui`.
- `src/jarn/repl/module_panel.py` — same pattern (`_C_ACCENT`, `_C_ON`,
  `_C_ERR`).
- Slash-command bodies — `[cyan]` in help, skills, memory, MCP, modules
  (`extensibility/commands.py`, `controller/commands/*.py`). Cyan is close
  to the default accent but **not** the configured accent.

### 3.2 There is no shared layout primitive

Almost every command builds a string:

```text
lines = ["[b]Title[/b] [dim](hint)[/dim]"]
lines.append(f"  [cyan]{name}[/cyan] — {desc}")
return CommandResult("\n".join(lines))
```

Seen in `/status`, `/cost`, `/sessions`, `/skills`, `/memory`, `/mcp`,
`/doctor`, `/help`, `/config`. There is no helper for:

- section header + blank line
- key/value column
- success / warn / error one-liners
- usage errors
- truncated path / model id
- “N more…” footers

Spacing is therefore accidental: some commands start with a title and no
leading blank; doctor inserts `\n[b]Providers[/b]` mid-stream; help has no
blank line between groups beyond the title line itself.

### 3.3 Command language is inconsistent

From `src/jarn/commands/registry.py`:

| Issue | Example |
|---|---|
| Implementation leaking into the description | `/login` “maps to `jarn auth login`” |
| Usage grammar is a private dialect | `/model [/ref\|refresh]`, `/memory [search\|show\|…] …` |
| Duplicate entry points unexplained | `/modules` vs `/module`, `/new` vs `/clear`, `/quit` vs `/exit` |
| Help groups are coarse | Daily / Setup / Session — `/wiki` sits in Session |
| No `/help compact` (command-level help) | User must scan the whole dump |
| Skills are not commands | Must type `/skill name` |
| Case sensitive | `/Help` is unknown |
| Shortcut legend is a paragraph | `HELP_SHORTCUTS` + `HELP_GLYPH_LEGEND` |

Unknown-command already has difflib suggestions (`controller/core.py`
`handle_command`). Keep that; restyle it.

### 3.4 Turn stream grammar is implicit

`src/jarn/repl_renderer.py`:

- User echo: `›` in `C_USER` (`repl/keys.py`, `repl/app.py`).
- Thinking: `✻ thinking` dim.
- Tool start: `⏺ **name**  dim args`.
- Tool end: indented `⎿ summary · 1.2s · ctrl+o`.
- Subagent: `┊ name` prefix; live `└ name: working…`; finish
  `┊ name ⎿ done · N tool calls`.
- Width hard-capped at **100 columns** (`_current_width`), so ultrawide
  terminals wrap early but the live region cannot use the extra space.
- Same-kind events share no blank line (`_sep`); kind changes insert one.
  Consecutive tools therefore pack tightly — good — but a tool followed by
  prose can still feel glued to the previous block on some paths.
- No density knob. Every tool start+end always prints.

Hermes default is quieter (progress line with emoji + duration; `/verbose`
to expand). J.A.R.N. should keep `⏺` / `⎿` (already in muscle memory) but
make density configurable and stop treating `ctrl+o` as the only discoverable
hint (`/expand` already exists).

### 3.5 Toolbar is information-rich and visually flat

`src/jarn/tui/toolbar.py` already collapses by priority. Missing vs Hermes:

- No `12.4K/200K` pair — only `ctx 42%`.
- No glyph fill bar.
- Context colors trip at 70% / 90%, not 50 / 80 / 95.
- No elapsed session time.
- No compression-count badge.
- YOLO is a mode glyph (`⚠ yolo`) but easy to miss among other segments.
- `🔒 trusted` uses an emoji; untrusted is `⚠ untrusted · jarn trust`.
- Cost line is `$0.0123 · 1,204 tok · 3 calls` — precise, long, low priority
  so it often drops on narrow terminals (priority 8). Hermes keeps cost
  longer because it is the “am I burning money” signal.

### 3.6 Splash orients weakly

`src/jarn/tui/logo.py`: ASCII wordmark + tagline + one shortcut hint.
It does **not** state model, cwd, permission mode, or loaded skills/tools.
Hermes’ first frame does. `ui.splash` is `full | compact | off` — compact is
the default, which is a single line and even less context.

### 3.7 CLI help is complete and overwhelming

`src/jarn/cli.py` `build_parser()` is thousands of lines. The epilog (from
line 249) is actually one of the better pieces — grouped examples. The
generated argparse option list above it is not. Errors use `ErrorDetail.render()`
(`src/jarn/errors.py`): code, cause, component, next, log — correct for
support, visually a brick in a TTY.

### 3.8 Telegram is a third dialect

Gateway HTML (`src/jarn/telegram/outbox.py`, `htmlutil.py`) already chose
quiet tool progress (off). Command names should stay the same as the REPL.
Copy and section structure should come from the same helpers, with an HTML
adapter — not a second set of titles.

### 3.9 Tests that will move with this work

Any visual/help change must update, not fight:

- `tests/test_phase3.py` — `format_help` groups, shortcuts, glyph legend,
  README command-row parity.
- `tests/test_controller.py` — Rich markup validity of `/help`.
- `tests/test_repl.py` — help body from registry.
- `tests/test_splash.py` — `/help` in shortcut hint.
- `tests/test_ux.py` — palette configure, blank-line `_sep` contract,
  `NO_COLOR`.
- `tests/test_terminal_contract.py` — dumb/`NO_COLOR` must stay escape-free.
- `tests/test_keycommand.py` — `/key` listed in help.
- README.md / README-TH.md command tables (parity tests exist).

---

## 4. Target visual grammar

One document, one module. Everything that prints to a human goes through it.

### 4.1 Roles, not raw colors

Extend `palette.py` (do not add a second palette). Every role below is a
token that already maps per theme, or a new one added next to it.

| Role | Meaning | Typical glyph |
|---|---|---|
| `accent` | Brand, command names, selected item | — |
| `user` | User-authored text | `›` prompt |
| `tool` | Agent tool activity | `⏺` start |
| `muted` | Secondary / meta (`C_DIM`) | `⎿` result |
| `success` | Completed ok, trusted, key ok | `✔` |
| `warn` | Degraded, untrusted, budget warn | `⚠` |
| `error` | Failed, denied, missing | `✗` |
| `notice` | Informational highlight | `·` |
| `plan` / `ask` / `auto-edit` / `yolo` | Permission modes | `◇` `◆` `⚡` `⚠` |

**Rule:** no `[cyan]`, `[green]`, `[red]`, `[yellow]`, and no hex literals
outside `palette.py`. Panels (`ConfigPanel`, `ModulePanel`) read the live
palette, not module-level constants.

**Rule:** `NO_COLOR` and `TERM=dumb` already strip styles in `styled_fg` /
`no_color()`. Layout helpers must go through the same gate so doctor/help
cannot leak ANSI.

### 4.2 Spacing (the actual “it looks messy” bug)

Adopt a four-level rhythm. Implement as helpers so people stop inventing
new gaps.

| Level | When | How |
|---|---|---|
| 0 | Inside a tight list (consecutive tools, consecutive `/help` rows) | no blank line |
| 1 | Between sections of one command (`Providers` → `Main model`) | one blank line |
| 2 | Between user prompt, tool block, and assistant prose | one blank line (already `_sep` on kind change — keep, audit misses) |
| 3 | After a finished turn, before the next `›` | one blank line, never two |

Forbidden: double blank lines; a title jammed against the previous command’s
last line; a usage error with no leading blank when printed after other
output.

Column layout for lists:

```text
  /model [name]          Switch the active model
  /mode  [plan|ask|…]    Switch permission mode
```

Command column is left-aligned and padded to the longest visible usage in
that section (cap ~28 chars, then wrap the description). Not
`/name usage — description` on one overflowing line.

Key/value blocks (`/status`, doctor):

```text
  Directory     /home/you/proj
  Model         openrouter/anthropic/claude-sonnet-4.5
  Permissions   Ask before changes  ·  ask
```

Label column ~14–16 chars, values wrap. Dim the labels, bright the values.

### 4.3 Type scale (Rich markup)

| Element | Markup |
|---|---|
| Page title | `[b]{accent}Title[/]` |
| Section | `[b]Section[/]` + blank line before |
| Command / name | accent, not bold unless selected |
| Description | default fg |
| Hint / shortcut | `C_DIM` |
| Destructive | `C_ERROR` bold |
| Success one-liner | `C_SUCCESS` + `✔` |

No rainbow. A screen should use accent + muted + one semantic color.

### 4.4 Prompt and stream glyphs (keep, document, stop growing)

Freeze this set. New glyphs need a spec amendment.

```text
›     user prompt (input and echo)
!     host shell escape (error-colored while typing — stays)
⏺     tool started
⎿     tool result / continuation
┊     subagent / nested
✻     thinking
◇ ◆ ⚡ ⚠   permission modes (plan ask auto-edit yolo)
✔ ⚠ ✗     success / warn / fail
```

Do **not** add per-tool emoji (💻🔍📄). J.A.R.N. is calmer than Hermes here.
If a density mode wants an icon, use the tool’s first letter in a dim
bracket: `[t] terminal`, not emoji.

### 4.5 Width

- Drop the hard 100-column cap for committed markdown, or raise it to
  `min(terminal, 120)` with a config `ui.wrap_at` (default 120, 0 = terminal
  width). Live preview may stay slightly narrower to reduce reflow cost.
- Toolbar: keep priority dropping; add Hermes-like breakpoints
  (≥76 full, 52–75 compact, &lt;52 model + duration + YOLO).
- `/help` in &lt;60 columns: name on one line, description indented on the next.

### 4.6 Context pressure colors

Align toolbar + `/context` + `/cost` to one ramp:

| Fraction | Color | Copy |
|---|---|---|
| &lt; 50% | `CTX_OK` (retint toward success-green in dark theme) | plenty |
| 50–80% | `CTX_WARN` | filling |
| 80–95% | new `CTX_HOT` (orange; add to palette) | near compact |
| ≥ 95% | `CTX_EXCEEDED` | compact now |

`/compact` hint appears at HOT, not only at EXCEEDED.

---

## 5. Shared layout module

**New:** `src/jarn/tui/layout.py`

Small, pure functions returning Rich markup strings (and a parallel HTML
adapter used by Telegram). No prompt_toolkit, no Console I/O.

```python
title(text) -> str
section(text) -> str          # leading blank + bold header
kv(label, value, *, label_width=14) -> str
row(name, description, *, name_width=24, name_style=accent) -> str
bullet(text, *, glyph="·") -> str
banner_ok / banner_warn / banner_err(text) -> str
usage(command, syntax, examples: list[str], related: list[str]) -> str
hint(text) -> str             # dim one-liner
rule(width) -> str            # optional, dim, only for doctor/status pages
truncate(text, width) -> str
pad_columns(rows: list[tuple[str, str]]) -> list[str]
```

`CommandResult.text` stays a string. Handlers switch from hand-rolled markup
to these helpers. Snapshot tests compare helper output, not 40 unique
string shapes.

Telegram: `layout.html` wrappers (`<b>`, `<code>`, `<i>`) with the same
function names so `/status` can do `status_lines(ctrl, dialect="rich"|"html")`.

---

## 6. Command language standard

### 6.1 Voice

Write descriptions as **what happens to the user**, present tense, no
implementation.

| Now | Target |
|---|---|
| Show or switch the active model; /model refresh re-queries local endpoints. | Show or switch the model. |
| Sign in or re-verify ChatGPT authentication (maps to `jarn auth login`). | Sign in to ChatGPT. |
| Open the last turn's full tool output in the pager (same as Ctrl+O). | Show the last tool output in full. |
| Show the ranked repo map (codebase overview). | Show a map of this repository. |

Details belong in `/help model`, not in the index line.

### 6.2 Usage syntax (one dialect)

Document in `/help` footer and CONTRIBUTING:

| Token | Meaning |
|---|---|
| `name` | required word |
| `[name]` | optional |
| `a\|b\|c` | one of |
| `<path>` | user value |
| `…` | more args, see `/help cmd` |

Examples:

- `/model [name\|refresh]`
- `/mode [plan\|ask\|auto-edit\|yolo]`
- `/memory search <query>`
- `/queue steer <n>`

Stop putting nested usage in the index (`[search|show|add|update|delete|dump] ...`).
The index shows `/memory [subcommand]` and `/help memory` lists subcommands.

### 6.3 `/help` information architecture

**Index (`/help`)**

```text
Commands                          type /help <name> for details

Work
  /model [name]                   Switch the model
  /mode [plan|ask|auto-edit|yolo] Switch how much J.A.R.N. may change
  /commit                         Commit the current diff (asks first)
  /review                         Read-only review of the current diff
  /undo  /redo                    Revert or restore the last file changes
  /abort                          Stop this turn and roll back its files

Session
  /status                         Where you are: model, mode, context
  /cost                           Tokens and estimated cost
  /context                        What is filling the context window
  /compact                        Summarize and continue in a fresh thread
  /clear                          Start over (alias: /new)
  /sessions                       Resume a previous session
  …

Setup
  /config                         Open settings
  /login  /logout                 ChatGPT sign-in
  /doctor                         Check config, keys, and extensions
  …

Shortcuts
  Tab complete · Shift+Tab mode · Esc stop turn · Ctrl+O expand output
  ! command   run a host shell line (no agent — stays red)

Permission modes    ◇ plan   ◆ ask   ⚡ auto-edit   ⚠ yolo
```

Groups become **Work / Session / Setup** (rename Daily → Work). Order by
frequency, not alphabet.

**Detail (`/help compact`)**

```text
/compact

  Summarize this conversation and keep going in a new thread.
  Auto-compact can also fire when the context window fills up.

  Usage     /compact
            /compact status

  Related   /clear   /context   /cost
```

Unknown command:

```text
Unknown command: /modle

  Did you mean  /model  /mode  /module?

  Type /help to list commands.
```

### 6.4 Case and aliases

- Parse slash names case-insensitively (`/Help`, `/HELP`).
- Keep hyphen/underscore equivalence (`add-dir` / `add_dir`) as today.
- Aliases stay: `/new` → `/clear`, `/exit` → `/quit`. Index shows the
  primary and mentions the alias in `/help <primary>` only, not as a second
  full row — unless the alias is typed as often as the primary (`/exit` can
  remain a one-line alias row).
- Skills: every loaded skill name is a slash command that routes to
  `cmd_skill`. Built-in names still win (existing `-custom` suffix rule).

### 6.5 New commands (display-only or thin wrappers)

| Command | Behavior |
|---|---|
| `/help [name]` | Index or detail page |
| `/verbose` | Cycle tool progress: `off → new → all → verbose` |
| `/focus [on\|off\|status]` | Hide tool chrome; remember previous `/verbose` |
| `/context [all]` | Visual context breakdown (logic already partly in `/cost`) |
| `/usage` | Alias of `/cost` |
| `/tools` | List tools the agent can use this session |
| `/title [text]` | Set session title (today inferred from first prompt) |

Not in v1 of this work: `/skin`, `/busy`, `/diff` (nice-to-have wave 5),
Hermes `/background` (J.A.R.N. already has `/ps` + `run_in_background`).

### 6.6 Completer

`CompletionProvider` already returns `label` + `description`. Show
description as `display_meta` (already wired). After the help rewrite,
meta text is the short index line, not the old implementation sentence.
Optional: grouped completion headers if prompt_toolkit allows without
jank — otherwise skip.

---

## 7. Target layouts (before → after)

### 7.1 Startup

**After (compact default, recommended):**

```text
JARN  v1.0.9  ·  just a reliable nerd

  Model     openrouter/anthropic/claude-sonnet-4.5
  Folder    ~/src/jarn
  Mode      ◆ Ask before changes
  Skills    3 loaded   ·  type /skills

  Type a message.  /help for commands.  Tab completes.  Shift+Tab changes mode.
```

Full splash keeps the ASCII wordmark **above** this strip. `ui.splash: off`
keeps only the strip (never a blank first frame).

### 7.2 Toolbar

**After, wide:**

```text
 claude-sonnet-4.5  │  ◆ ask  │  12.4K/200K  [██████░░░░] 6%  │  $0.06  │  15m
```

YOLO appends a persistent `⚠ YOLO` in `C_ERROR` that is never dropped before
the model name. Untrusted: `⚠ untrusted` stays high priority.

**Compact:** model · bar · cost. **Minimal:** model · YOLO.

Fill bar uses 10 characters, `█` / `░`, colored by the ramp in §4.6.

### 7.3 Turn stream (`ui.tool_progress: new` default)

```text
› Fix the flaky toolbar test

  ✻ thinking

  ⏺ pytest  tests/test_toolbar.py
    ⎿ 1 failed · 2.4s

  The failure is a width-collapse off-by-one in render_toolbar.

  ⏺ edit  src/jarn/tui/toolbar.py
    ⎿ updated render_toolbar · 0.4s

  Patched the priority sort so the cost segment can be kept on 72-column
  terminals. Run /expand to see the full pytest output.
```

`off`: only the final assistant text (plus `/focus` recovery line).
`all`: current behavior (every start + end + live tail).
`verbose`: also print truncated stdout inline, not only in `/expand`.

Thinking stays dim and collapses to one `✻ thinking · 1.2s` line when the
model starts tools, unless `ui.show_reasoning: true`.

### 7.4 `/status`

```text
Status

  Directory     /home/you/src/jarn
  Model         openrouter/anthropic/claude-sonnet-4.5
  Provider      openrouter  ·  API key
  Reasoning     provider default
  Permissions   Ask before changes  ·  ask
  Workspace     trusted
  Context       12,400 / 200,000  [██████░░░░] 6%
  Session       a1b2c3d4  ·  15m  ·  4 turns

Recap
  Tools         edit 6 · read 11 · shell 3
  Files         src/jarn/tui/toolbar.py · tests/test_toolbar.py
  Last you      Fix the flaky toolbar test
  Last J.A.R.N. Patched the priority sort…
```

Recap is local (session index + last messages). No extra model call.

### 7.5 `/doctor`

Keep the same checks. Restyle as sections with `✔ / ⚠ / ✗` from the palette,
not `[green]`. Closing line stays one of:

- `✔ All good.`
- `⚠ n issues — see above. Run jarn doctor --fix --dry-run to preview repairs.`

### 7.6 `jarn --help`

Keep argparse for flags (tests introspect it). Change presentation:

1. Short description.
2. **Common commands** (today’s epilog, first).
3. Then “All options” collapsed conceptually: group with argparse
   `argument_groups` (Start, Output, Auth, Gateway, …) so `--help` is
   scannable.
4. Last line: `jarn <command> --help` for details.

TTY errors from `ErrorDetail.render()`: blank line between fields; color
the code (`C_ERROR`) and `Next:` (`accent`) when stdout is a TTY;
unchanged plain text when not (JSON / pipes / `NO_COLOR`).

---

## 8. Config keys

Add under `ui:` (defaults chosen for quiet-but-honest):

```yaml
ui:
  theme: dark
  accent: cyan
  splash: compact
  wrap_at: 120                 # 0 = use terminal width
  tool_progress: new           # off | new | all | verbose
  show_reasoning: collapsed    # collapsed | full | off
  statusbar: true
  context_bar: true
```

`/verbose` and `/focus` mutate `tool_progress` for the session; persist only
if the user `/config set`s it (Hermes sometimes persists — J.A.R.N. should
not surprise people by writing YAML on a cycle command).

Schema + pydantic + `SETTINGS` + `CONFIGURATION.md` + defaults template
must ship in the same wave as the first consumer.

---

## 9. Implementation waves

Do not land “a bit of everything”. Each wave is reviewable, test-gated, and
leaves the product consistent.

### Wave A — Foundation (no user-visible copy rewrite yet)

**Intent:** make it impossible to print a new unthemed color.

| Task | Files | Tests |
|---|---|---|
| A1. Add missing palette tokens (`CTX_HOT`, `C_LABEL`) | `tui/palette.py`, `tui/theme.py` | `tests/test_ux.py` theme switch |
| A2. `tui/layout.py` helpers + HTML dialect | new module | new `tests/test_layout.py` (markup, padding, `NO_COLOR`) |
| A3. Replace hardcoded hex in ConfigPanel / ModulePanel | `config/settings.py`, `repl/module_panel.py` | existing panel tests; assert colors follow `configure_ui` |
| A4. Doctor uses palette tokens via layout | `doctor/render.py` | doctor tests still match structure, not `[green]` |
| A5. Ban named Rich colors in `src/jarn/**` except palette | lint test | `tests/test_palette_discipline.py` greps `[green]` / `#22d3ee` outside allowlist |

**Acceptance:** `ui.theme: light` retints doctor, `/config` panel, and
`/modules` panel. `NO_COLOR=1` still emits zero CSI.

**Risk:** doctor tests that snapshot `[green]✔`. Update snapshots in A4.

### Wave B — Help & command copy

| Task | Files | Tests |
|---|---|---|
| B1. Registry: short `description`, optional `blurb`, `examples`, `related`, cleaner `usage` | `commands/registry.py` | phase3 / README parity |
| B2. `format_help` column layout + `/help &lt;name&gt;` | `extensibility/commands.py`, `controller/commands/meta.py` | `test_phase3.py`, `test_controller.py` |
| B3. Case-insensitive dispatch | `controller/core.py`, `extensibility/commands.py` `parse_input` | new cases in `test_extensibility.py` |
| B4. `usage_error()` for every `Usage: /foo` string | `controller/commands/*` | per-command tests keep the syntax, not the old sentence |
| B5. Skills as slash commands | `extensibility/commands.py` completion catalog, `handle_command` | skills tests + completion tests |
| B6. README.md + README-TH.md command tables | docs | existing parity test |

**Acceptance:** `/help` is ≤ ~40 lines at 80 columns; `/help compact` explains
auto-compact; `/HELP` works; `/skillz` suggests `/skills`.

**Copy review:** do B1 in one PR even if B5 slips — the registry is the
source of truth.

### Wave C — Live session chrome

| Task | Files | Tests |
|---|---|---|
| C1. Splash info strip | `tui/logo.py`, REPL boot | `test_splash.py` |
| C2. Toolbar: used/max, fill bar, duration, YOLO sticky, new breakpoints | `tui/toolbar.py`, REPL clock | toolbar unit tests (new file if needed) |
| C3. Context color ramp 50/80/95 | palette + toolbar + `/context` | ramp unit tests |
| C4. `ui.tool_progress` + `/verbose` + `/focus` | renderer, registry, config | renderer tests for each density |
| C5. Wrap width `ui.wrap_at`; relax 100-col cap | `repl_renderer.py`, `repl/app.py` | width tests |
| C6. Collapse thinking by default | renderer | existing reasoning tests |

**Acceptance:** 72-column terminal shows model + bar + YOLO. `/focus on`
hides `⏺` lines and prints `⋯ n tool lines hidden · /focus off`. Default
`new` still shows each distinct tool once (start or end, not a wall).

**Risk:** live-sink + Rich Live dual paths. Change `_format_tool_progress`
and `on_tool` behind the density flag; keep `all` as today’s pixels so
regressions are diffable.

### Wave D — Command pages people actually open

| Task | Files | Tests |
|---|---|---|
| D1. Restyle `/status` + recap | `controller/commands/diagnostics.py` | new recap tests (fake session stats) |
| D2. Split `/context` from `/cost`; alias `/usage` | diagnostics + cost tracker | cost tests still pass; context table tests |
| D3. Restyle `/sessions`, `/skills`, `/memory` list, `/mcp` | respective command modules | existing tests, markup via layout helpers |
| D4. `/tools` list from the live runtime | new handler | mocked runtime |
| D5. `/title` | session index field already exists for inferred titles | session tests |

**Acceptance:** `/status` answers directory, model, mode, context, and last
turn without running a model. `/context` shows a bar + category table.

### Wave E — CLI + doctor + Telegram

| Task | Files | Tests |
|---|---|---|
| E1. argparse argument groups; epilog first in custom formatter if needed | `cli.py` | `test_ga_cli_admin.py`, gateway CLI help tests |
| E2. TTY-colored `ErrorDetail.render` | `errors.py` | keep exact plain-text anatomy for non-TTY |
| E3. Doctor closing CTA + section spacing (content already A4) | `doctor/render.py` | doctor tests |
| E4. Telegram uses layout HTML dialect for `/status` `/help` `/cost` | `telegram/outbox.py` or bot command path | telegram package tests |
| E5. `CONFIGURATION.md` ui keys; `docs/README.md` link | docs | none |

### Wave G — Live-stream layout helpers

Move turn-stream chrome (`›`, `!`, `⏺`, `⎿`, `┊`, `✻`, todos, host-shell
banner) into `layout.py` so the renderer, REPL, and Telegram cannot drift.
Callers must not pre-escape; `layout.py` is the only module that may import
`rich.markup.escape` or compose `[color]` tags.

### Wave H–I — REPL consumers

`repl_renderer.py`, `repl/turn.py`, `repl/keys.py`, `repl/app.py`,
`repl/commands.py`, overlays, auth errors, and `Controller.status_line`
print through layout helpers. prompt_toolkit HTML (toolbar, menus) and
Rich `Text` (diff widget, live thinking) stay specialized and read
`grammar` + `palette` directly.

### Wave J — CLI subcommand groups + plain pages

Busy parsers (`exec`, `doctor`, `gateway`, `config`, `auth`, `sessions`,
`update`) use named `add_argument_group`s. Human `Label: value` pages
(`config reset` preview, `telemetry status`) use `layout.field(..., dialect="plain")`.
JSON / raw paths stay unstyled. `jarn --help` stays ≤ 160 lines.

### Wave K — Telegram command layers

`GATEWAY_READONLY_COMMANDS` (display pages) ∪ `GATEWAY_SESSION_COMMANDS`
(`verbose` / `focus` / `title`, no YAML write) = `GATEWAY_LOCAL_COMMANDS`.
Mutating names (`config`, `preset`, `memory`, `sandbox`) stay out.

### Wave F — Polish (optional, after A–E)

- `/diff` (staged / all / session) if checkpoint + git already make it cheap.
- Paste preview for huge multiline pastes (Hermes one-liner).
- Interactive `/sessions` filter (today `/resume` is the picker — maybe
  merge copy so `/sessions` *is* the picker and the list is the empty-query
  state).
- Glyph-pack / `ui.prompt_symbol` if users ask; not required for the
  standard.
- Screenshot or `script` fixtures in docs/assets for the new default look.

---

## 10. File-level ownership (quick map)

| Area | Primary files |
|---|---|
| Tokens | `src/jarn/tui/palette.py`, `theme.py` |
| Layout helpers | `src/jarn/tui/layout.py` **(new)** |
| Toolbar | `src/jarn/tui/toolbar.py` |
| Splash | `src/jarn/tui/logo.py` |
| Turn stream | `src/jarn/repl_renderer.py`, `src/jarn/repl/app.py`, `repl/keys.py` |
| Command catalog | `src/jarn/commands/registry.py` |
| Help text | `src/jarn/extensibility/commands.py` |
| Command bodies | `src/jarn/controller/commands/*.py` |
| Doctor | `src/jarn/doctor/render.py` |
| CLI | `src/jarn/cli.py`, `src/jarn/errors.py` |
| Config | `schema.py`, `pydantic_schema.py`, `defaults.py`, `settings.py` |
| Telegram | `src/jarn/telegram/outbox.py`, `htmlutil.py` |
| Docs | `README.md`, `README-TH.md`, `docs/CONFIGURATION.md`, `docs/QUICKSTART.md` |

---

## 11. Testing strategy

1. **Golden markup tests** for `layout.py` (padding, wrapping, `NO_COLOR`).
2. **Palette discipline test** — fail CI if `src/jarn` grows `[green]` /
   raw `#22d3ee` outside `palette.py`.
3. **Help parity** — README tables generated from `readme_command_rows()`
   still match; extend the row shape if we add a short-description field.
4. **Renderer density** — parametrize `off/new/all/verbose` against a fake
   event sequence; assert line counts and presence/absence of `⏺`.
5. **Toolbar width** — fixed widths 40 / 60 / 80 / 120; YOLO never absent
   when mode is yolo; fill bar absent below compact breakpoint.
6. **Terminal contract** — existing `test_terminal_contract.py` must stay
   green; new helpers included.
7. **No behavior change in wave A** except colors. Commands still return
   the same facts.

Manual UAT (wave C+): 80x24, 120x40, `NO_COLOR=1`, `ui.theme=light`,
Telegram `/status`, and a YOLO session to confirm the badge.

---

## 12. Compatibility and rollout

- **No breaking CLI flags.** New `ui.*` keys default to the new quiet look.
  Users who want today’s tool wall set `ui.tool_progress: all`.
- **Do not persist `/verbose` cycles** unless `/config set`.
- **Slash command names stay.** We add aliases (`/usage`) and details
  (`/help cmd`); we do not rename `/cost` → `/usage` as the only spelling.
- **Telegram** stays HTML, tool progress off by default (already). Help
  index must fit Telegram’s message limit — use `chunk_html`.
- **Headless `-p` / `jarn exec`** are not restyled beyond shared error
  rendering; JSON output stays JSON.
- Ship waves as sequential PRs. Do not mix a help rewrite with a renderer
  rewrite in one review.

---

## 13. Suggested PR sequence

1. **A1–A5** — “ui: one palette and layout helpers”
2. **B1–B6** — “ux: readable /help and command copy”
3. **C1–C6** — “ux: splash, toolbar bar, quiet tool stream”
4. **D1–D5** — “ux: status, context, tools, title”
5. **E1–E5** — “ux: CLI help groups, doctor CTA, Telegram dialect”
6. **G–K** — live-stream helpers, REPL/CLI consumers, Telegram command layers,
   palette-markup discipline (this follow-up)

Each PR description should include a before/after text mockup (no screenshots
required) and the wave’s acceptance lines from this spec.

---

## 14. Success criteria (the whole program)

A person who has used Hermes but never J.A.R.N. can:

1. Launch `jarn` and know the model, folder, and mode without typing
   `/status`.
2. Type `/help`, find `/mode`, and understand ask vs yolo without reading
   PERMISSIONS.md.
3. Watch a turn and follow *what happened* from `⏺` / `⎿` without a glyph
   legend.
4. Glance at the toolbar and know context pressure and cost.
5. Run `jarn doctor` and see pass/fail in the same colors as the rest of
   the app, including light theme.
6. Hit a typo and get a suggestion, not a brick of argparse.
7. Use `/HELP`, `/usage`, and `/skill-name` without learning a second
   dialect.

J.A.R.N. still looks like J.A.R.N.: cyan/teal, permission glyphs, red `!`
shell escape, native scrollback. It just stops looking unfinished next to
Hermes.
