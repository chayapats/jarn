# Hermes display-parity UAT (P5-1 / P5-2)

Acceptance checklists from
[2026-08-15-hermes-parity-closeout.md](../specs/2026-08-15-hermes-parity-closeout.md)
§2 / P5-1 / P5-2.

**This file is evidence, not a live run log.** Status values:

| Status | Meaning |
|---|---|
| **Covered** | Behavior is asserted in pytest on the stacked **code** PR. The test file is named. This docs PR did not re-run that suite as a merged tree. |
| **Not run** | Live terminal, live Telegram bot, or a visual width/theme session was **not** executed here. |

Do **not** read Covered as Pass. No row below is Pass. No GitHub Release is claimed.

Code is a **stack of draft PRs** (not merged into `feat/hermes-parity-closeout`):

| Phase | PR | Branch |
|---|---|---|
| P0 docs contract | [#93](https://github.com/chayapats/jarn/pull/93) | `cursor/hermes-p0-docs-4113` |
| P1 CLI remaining | [#96](https://github.com/chayapats/jarn/pull/96) | `cursor/hermes-parity-p1-a555` |
| P4-1 CLI Enter-while-busy | [#97](https://github.com/chayapats/jarn/pull/97) | `cursor/p4-1-cli-enter-while-busy-702f` (onto P1) |
| P2 Telegram live turn | [#94](https://github.com/chayapats/jarn/pull/94) | `cursor/telegram-p2-live-turn-8286` |
| P3 Telegram commands | [#95](https://github.com/chayapats/jarn/pull/95) | `cursor/telegram-p3-commands-385b` (onto P2) |
| P4 Telegram busy UX | [#98](https://github.com/chayapats/jarn/pull/98) | `cursor/telegram-p4-busy-ux-7326` (onto P3) |
| P5 evidence | [#99](https://github.com/chayapats/jarn/pull/99) | `cursor/hermes-p5-evidence-29f0` |

Text mockups: [cli-paste-preview.md](cli-paste-preview.md),
[cli-sessions-picker.md](cli-sessions-picker.md),
[telegram-verbose-bubble.md](telegram-verbose-bubble.md),
[telegram-busy-ack.md](telegram-busy-ack.md).

---

## P5-1 CLI

Live geometry / theme rows stay **Not run**. Pytest-covered rows cite the file
on the P1 / P4-1 branches.

| # | Check | Status | Evidence |
|---|---|---|---|
| C1 | Launch 80×24: splash shows model, folder, mode, skills without `/status`; toolbar still readable | **Not run** | Live TTY |
| C2 | Launch 120×40: toolbar shows context fill bar + duration + cost; compact-count + title when those exist | **Not run** | Live TTY. Width drop order (YOLO > model > bar > title) is Covered in `tests/test_phase3.py` (`test_toolbar_title_drops_before_bar_and_yolo`, `test_toolbar_yolo_badge_survives_narrow`) on [#96](https://github.com/chayapats/jarn/pull/96) |
| C3 | `NO_COLOR=1`: no CSI; final answer does not leak `**` wrappers; fences/lists kept | **Not run** (live). Wrapper strip **Covered** | `tests/test_terminal_contract.py` (`test_term_dumb_disables_rich_and_prompt_toolkit_color`); `tests/test_ux.py` (`test_strip_md_wrappers_keeps_fences_and_lists`) on [#96](https://github.com/chayapats/jarn/pull/96) |
| C4 | `TERM=dumb`: same markdown-light contract as C3 | **Not run** (live). **Covered** in pytest | same as C3 |
| C5 | `ui.theme: light`: palette still cyan/teal identity, not a Hermes skin | **Not run** | Live TTY |
| C6 | YOLO session: YOLO badge cannot hide on a narrow toolbar | **Not run** (live). **Covered** in pytest | `tests/test_phase3.py` `test_toolbar_yolo_badge_survives_narrow` on [#96](https://github.com/chayapats/jarn/pull/96) |
| C7 | Default tool stream `new`: one ⏺ / ⎿ pair per distinct tool | **Not run** | Live turn. Density helper Covered in `tests/test_layout.py` `test_next_tool_progress_cycles` (A–K / this base) |
| C8 | `/verbose` cycles session density; does not write YAML | **Not run** | Live REPL |
| C9 | `/focus` hides tool chrome | **Not run** | Live REPL |
| C10 | Multiline paste: one dim preview line in scrollback; agent still gets the full text | **Not run** (live). Echo path **Covered** | `tests/test_repl_paste.py` (`test_placeholder_format`, `test_submitted_echo_multiline_without_token_is_one_line`) on [#96](https://github.com/chayapats/jarn/pull/96). Mockup: [cli-paste-preview.md](cli-paste-preview.md) |
| C11 | Host-direct `!` stays red on submit (including a pasted `!` payload) | **Not run** (live). **Covered** in pytest | `tests/test_repl_paste.py` `test_submitted_echo_host_direct_stays_red` on [#96](https://github.com/chayapats/jarn/pull/96) |
| C12 | `/sessions` is the interactive picker; `/resume` is the same | **Not run** (live). **Covered** in pytest | `tests/test_repl.py` `test_sessions_and_resume_open_the_picker` on [#96](https://github.com/chayapats/jarn/pull/96). Mockup: [cli-sessions-picker.md](cli-sessions-picker.md) |
| C13 | `/sessions [q]` filters title / label / thread-id prefix | **Not run** (live). **Covered** in pytest | `tests/test_session_label.py` `test_cmd_sessions_filters_by_query`; `tests/test_repl.py` `test_sessions_picker_filters_query` on [#96](https://github.com/chayapats/jarn/pull/96) |
| C14 | After `/resume` / continue: local recap (no model call) | **Not run** (live). **Covered** in pytest | `tests/test_status_recap.py` `test_resume_recap_reuses_status_scan_without_model` on [#96](https://github.com/chayapats/jarn/pull/96) |
| C15 | `/help` finds `/mode`; `/HELP` works | **Not run** (live). Registry **Covered** | `tests/test_phase3.py` `test_format_help_groups_contain_expected_commands`; `tests/test_layout.py` `test_help_detail_page` (A–K / this base). P1 README parity: `test_readme_commands_match_registry` on [#96](https://github.com/chayapats/jarn/pull/96) |
| C16 | `/diff [staged\|all\|session]` prints a colored unified diff; unknown subcommand → usage | **Not run** (live). **Covered** in pytest | `tests/test_diff_render.py` `test_cmd_diff_staged_all_and_unknown`, `test_cmd_diff_session_uses_recap_files` on [#96](https://github.com/chayapats/jarn/pull/96) |
| C17 | `/busy [interrupt\|queue\|steer\|status]`; session-only; persist only via `/config set ui.busy_input_mode` (default `queue`) | **Not run** (live). Config **Covered** | `tests/test_config.py` `test_ui_busy_input_mode_default_queue` on [#96](https://github.com/chayapats/jarn/pull/96) |
| C18 | `/busy queue` then Enter during a turn queues; `/busy steer` then Enter steers; `/busy interrupt` then Enter aborts then runs | **Not run** (live). **Covered** in pytest | `tests/test_repl.py` `test_busy_enter_queue_enqueues_without_steer`, `test_busy_enter_steer_sets_slot_not_queue`, `test_busy_enter_interrupt_aborts_then_runs_line` on [#97](https://github.com/chayapats/jarn/pull/97) |
| C19 | `/usage` aliases `/cost` (no second spelling) | **Not run** (live). **Covered** | registry `alias_of="cost"`; `tests/test_phase3.py` README/help parity (A–K / this base) |
| C20 | Background job exit prints a small scrollback panel (id, exit, tail, `/ps` hint) | **Not run** (live). **Covered** in pytest | `tests/test_background.py` `test_background_finish_panel_text`, `test_exit_listener_fires_on_prune` on [#96](https://github.com/chayapats/jarn/pull/96) |

---

## P5-2 Telegram

Live bot rows stay **Not run**. Pytest-covered rows cite the P2 / P3 / P4 branches.

| # | Check | Status | Evidence |
|---|---|---|---|
| T1 | Default turn is quiet: prose draft → finalize only; tools off | **Not run** (live). **Covered** in pytest | `tests/test_telegram_outbox.py` `test_tool_progress_ignored`, `test_off_emits_zero_tool_messages` on [#94](https://github.com/chayapats/jarn/pull/94) |
| T2 | `gateway.telegram.tool_progress` unset/off does **not** inherit CLI `ui.tool_progress: new` | **Not run** (live). **Covered** in pytest | `tests/test_telegram_outbox.py` `test_effective_telegram_tool_progress_does_not_inherit_ui`; `tests/test_gateway_worker.py` `test_worker_seeds_telegram_progress_off_not_ui_new` on [#94](https://github.com/chayapats/jarn/pull/94) |
| T3 | `/verbose` then a turn: one HTML progress bubble edited in place (`editMessageText`); density `new` is one ⏺/⎿ per tool | **Not run** (live). **Covered** in pytest | `tests/test_telegram_outbox.py` `test_new_density_one_bubble_edited_in_place`, `test_verbose_then_tool_start_appears`; `tests/test_gateway_worker.py` `test_verbose_then_tool_start_is_not_dropped` on [#94](https://github.com/chayapats/jarn/pull/94). Mockup: [telegram-verbose-bubble.md](telegram-verbose-bubble.md) |
| T4 | Progress bubble disappears when the answer lands (`tool_progress_cleanup: delete`) | **Not run** (live). **Covered** in pytest | `tests/test_telegram_outbox.py` `test_cleanup_deletes_progress_bubble_on_done`, `test_cleanup_keep_leaves_progress_bubble` on [#94](https://github.com/chayapats/jarn/pull/94) |
| T5 | Subagent inner stream (`data.agent`) stays dropped even when progress is `new` | **Not run** (live). **Covered** in pytest | `tests/test_telegram_outbox.py` `test_subagent_tools_dropped_even_when_progress_new` on [#94](https://github.com/chayapats/jarn/pull/94) |
| T6 | Approval cards still destroy the prose draft; progress `message_id` ≠ draft id | **Not run** (live). **Covered** in pytest | `tests/test_telegram_outbox.py` `test_progress_bubble_does_not_destroy_prose_draft`, `test_approval_card_still_destroys_draft_not_progress` on [#94](https://github.com/chayapats/jarn/pull/94) |
| T7 | Quiet long turn: `Working — N min` after ~3 quiet minutes unless `long_running_notifications: false` | **Not run** (live). Fake-clock **Covered** | `tests/test_telegram_outbox.py` `test_long_running_heartbeat_after_interval`, `test_long_running_heartbeat_disabled` on [#94](https://github.com/chayapats/jarn/pull/94) |
| T8 | `/status` `/help` `/cost` run locally (HTML pages; `/help` chunks at 4096) | **Not run** (live). **Covered** in pytest | `tests/test_gateway_worker.py` `test_local_slash_status_does_not_run_agent`; `tests/test_telegram_bot.py` `test_help_uses_command_catalog_html_and_does_not_submit_a_turn`, `test_help_chunks_oversized_html` on [#95](https://github.com/chayapats/jarn/pull/95) |
| T9 | `/model` `/mode` local; trusted YOLO escalate opens the existing confirm card; cancel leaves mode; untrusted clamps with no granting card | **Not run** (live). **Covered** in pytest | `tests/test_gateway_worker.py` `test_mode_ask_is_local_not_an_agent_turn`, `test_mode_yolo_sends_card_cancel_leaves_mode`, `test_mode_yolo_untrusted_clamps_without_card`, `test_model_set_invalidates_runtime` on [#95](https://github.com/chayapats/jarn/pull/95) |
| T10 | `/sessions [q]` text list; `/resume <id>` local; unknown id is not an agent turn | **Not run** (live). **Covered** in pytest | `tests/test_session_label.py` `test_cmd_sessions_filters_by_query`; `tests/test_gateway_worker.py` `test_resume_unknown_id_does_not_start_agent_turn` on [#95](https://github.com/chayapats/jarn/pull/95) |
| T11 | `/compact` calls `controller.compact()`; `/undo` without confirm does not restore | **Not run** (live). **Covered** in pytest | `tests/test_gateway_worker.py` `test_compact_calls_controller_compact`, `test_undo_without_confirm_does_not_restore` on [#95](https://github.com/chayapats/jarn/pull/95) |
| T12 | Skill slash seeds `run_agent_turn`; unknown skill is a notice, not an agent prompt | **Not run** (live). **Covered** in pytest | `tests/test_gateway_worker.py` `test_skill_slash_seeds_same_turn`, `test_unknown_skill_is_notice_not_agent_prompt` on [#95](https://github.com/chayapats/jarn/pull/95) |
| T13 | `/config set …` (and other mutating names) refused with a terminal / `jarn` CLI hint; zero `submit_turn` | **Not run** (live). **Covered** in pytest | `tests/test_telegram_bot.py` `test_mutating_config_set_is_refused_without_submit_turn`; `tests/test_gateway_worker.py` `test_mutating_slash_on_worker_is_notice_not_agent` on [#95](https://github.com/chayapats/jarn/pull/95) |
| T14 | `/reset` aliases `/new`; `/rollback` is help copy, not a mutate | **Not run** (live). **Covered** in pytest | `tests/test_telegram_bot.py` `test_reset_aliases_new_and_does_not_submit_a_turn`, `test_rollback_is_help_alias_not_a_turn_or_mutate` on [#95](https://github.com/chayapats/jarn/pull/95) |
| T15 | BotFather menu ⊆ local + gateway-only names; no mutating names | **Not run** (live). **Covered** in pytest | `tests/test_telegram_bot.py` `test_botfather_menu_names_are_local_or_gateway_only` on [#95](https://github.com/chayapats/jarn/pull/95) |
| T16 | Second DM while a turn is in flight **steers** by default (does not inherit CLI queue); never a second `_run_turn` | **Not run** (live). **Covered** in pytest | `tests/test_gateway_worker.py` `test_busy_second_turn_steers_without_second_run`, `test_busy_second_turn_queue_does_not_double_run`; `tests/test_telegram_outbox.py` `test_effective_telegram_busy_input_mode_does_not_inherit_ui_queue` on [#98](https://github.com/chayapats/jarn/pull/98). Mockup: [telegram-busy-ack.md](telegram-busy-ack.md) |
| T17 | Busy DM: one short `Working…` ack; `ui.busy_ack_detail` / `gateway.telegram.busy_ack_detail` default off | **Not run** (live). **Covered** in pytest | `tests/test_telegram_outbox.py` `test_busy_ack_is_short_working_without_detail`, `test_busy_ack_detail_off_by_default_on_event` on [#98](https://github.com/chayapats/jarn/pull/98) |

---

## Safety (close-out must not reopen)

| # | Check | Status | Evidence |
|---|---|---|---|
| S1 | No remote ALWAYS on Telegram cards | **Not run** (live). **Covered** | `tests/test_telegram_outbox.py` `test_tool_card_has_once_session_deny_no_always` (v1 / this base; unchanged by P2–P4) |
| S2 | YOLO remains a card (CLI + Telegram) | **Not run** (live). Telegram **Covered** | T9 |
| S3 | Untrusted still clamped | **Not run** (live). Telegram **Covered** | T9 `test_mode_yolo_untrusted_clamps_without_card` |
| S4 | `!` stays host-direct and red | **Not run** (live). CLI **Covered** | C11 |

---

## Optional / skipped (not blockers)

| Item | Status | Notes |
|---|---|---|
| P4-4 `/background` | Deferred | `/ps` + background tools are sufficient ([#98](https://github.com/chayapats/jarn/pull/98)) |
| P4-5 Ctrl+S input stash | Skipped | Not required to close the program |
| P5-3 `demo.gif` | Not recorded | `vhs` was not installed in this environment. See [README.md](README.md) |
| P5-8 git tag / GitHub Release | Not done | Explicitly out of this program |

---

## How to run the live rows later

CLI (from a checkout of the P1 + P4-1 stack):

```bash
# 80×24 and 120×40 in a real TTY — splash, toolbar, YOLO, paste, /sessions
jarn
```

Telegram (from a checkout of the P2 + P3 + P4 stack, with a bot token you own):

```bash
jarn gateway setup
jarn gateway
# then DM: /status /help /cost /verbose + a turn /mode yolo /skill <name>
```
