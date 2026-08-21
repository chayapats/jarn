"""UI chrome locale catalog.

Callers look up chrome strings with :func:`t` and resolve ``ui.locale`` with
:func:`resolve_locale`. Slash command names, mode ids, model output, file
paths, git commands, and MCP tool ids are never translated.

This module is the catalog only. Callers (composer, activity renderer,
Telegram outbox) read keys via :func:`t`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

Locale = Literal["en", "th"]
LocaleSetting = Literal["auto", "en", "th"]

LOCALES: tuple[Locale, ...] = ("en", "th")
LOCALE_SETTINGS: tuple[LocaleSetting, ...] = ("auto", "en", "th")

# (key, English, Thai) — one row per chrome string so both catalogs stay aligned.
_STRINGS: tuple[tuple[str, str, str], ...] = (
    # §3.2 Composer
    (
        "composer.placeholder.first",
        "Ask jarn to plan, search, or build",
        "ให้ jarn วางแผน ค้นหา หรือลงมือ",
    ),
    ("composer.placeholder.later", "Message jarn", "พิมพ์ถึง jarn"),
    ("composer.interrupt", "esc to interrupt", "esc เพื่อหยุด"),
    # §3.3 Status line fragments (mode ids / YOLO stay English in both)
    ("toolbar.yolo", "YOLO", "YOLO"),
    ("toolbar.untrusted", "untrusted", "untrusted"),
    ("toolbar.trusted", "trusted", "trusted"),
    ("toolbar.queue", "queue {n}", "คิว {n}"),
    ("toolbar.compact", "compact {n}", "compact {n}"),
    # §3.4 Activity verbs / thinking
    ("tool.verb.read_file", "Read", "อ่าน"),
    ("tool.verb.edit_file", "Edit", "แก้"),
    ("tool.verb.write_file", "Write", "เขียน"),
    ("tool.verb.bash", "Run", "รัน"),
    ("tool.verb.shell", "Run", "รัน"),
    ("tool.result.lines", "{n} lines", "{n} บรรทัด"),
    ("tool.result.done", "done", "เสร็จ"),
    ("thinking.plain", "Thinking…", "คิด…"),
    # §3.5 Compact splash orientation
    (
        "splash.orientation",
        "Type a message. /help for commands.",
        "พิมพ์ข้อความได้เลย  ·  /help สำหรับคำสั่ง",
    ),
    # §3.6 Approval card nouns
    (
        "approval.header.edit",
        "Allow this edit to {object}?",
        "อนุญาตให้แก้ {object}?",
    ),
    (
        "approval.header.write",
        "Allow this write to {object}?",
        "อนุญาตให้เขียน {object}?",
    ),
    (
        "approval.header.shell",
        "Allow running {object}?",
        "อนุญาตให้รัน {object}?",
    ),
    (
        "approval.header.network",
        "Allow network to {object}?",
        "อนุญาตให้เชื่อมต่อ {object}?",
    ),
    (
        "approval.header.read",
        "Allow reading {object}?",
        "อนุญาตให้อ่าน {object}?",
    ),
    ("approval.once", "Allow once", "ครั้งนี้ครั้งเดียว"),
    ("approval.session", "Allow for this session", "ทั้งเซสชันนี้"),
    ("approval.always", "Always allow", "อนุญาตการแก้ไฟล์นี้ตลอด"),
    ("approval.deny", "Deny", "ปฏิเสธ"),
    ("approval.edit", "Edit before apply", "แก้ก่อนแล้วค่อยใช้"),
    ("approval.danger", "Dangerous", "อันตราย"),
    ("approval.nav", "↑/↓  Enter  Esc", "↑/↓  Enter  Esc"),
    ("approval.view_diff", "View full diff", "ดู diff เต็ม"),
    (
        "yolo.confirm",
        "YOLO will stop asking before edits and shell. The danger-guard still blocks.",
        "YOLO จะเลิกถามก่อนแก้ไฟล์และรันคำสั่ง อันตรายยังถูกบล็อก",
    ),
    (
        "yolo.confirm.prompt",
        "Type 'y' to confirm, anything else to cancel [y/N]: ",
        "พิมพ์ y เพื่อยืนยัน ไม่เช่นนั้นยกเลิก [y/N]: ",
    ),
    # §3.7 TTY error chrome (non-TTY anatomy stays English)
    ("error.next", "Next", "ถัดไป"),
    ("error.cause", "Cause", "สาเหตุ"),
    ("error.component", "Component", "ส่วนประกอบ"),
    ("error.log", "Log", "บันทึก"),
    ("error.report", "Report", "รายงาน"),
    ("error.retryable.yes", "yes", "ใช่"),
    ("error.retryable.no", "no", "ไม่"),
    (
        "error.log_unavailable",
        "unavailable for this failure (expected location: {path}; use `jarn doctor --report FILE`)",
        "ไม่มีสำหรับความล้มเหลวนี้ (ตำแหน่งที่คาด: {path}; ใช้ `jarn doctor --report FILE`)",
    ),
    # S09 — /help chrome (command names and mode ids stay English)
    ("help.title", "Commands", "คำสั่ง"),
    ("help.subtitle", "type /help <name> for details", "พิมพ์ /help <name> สำหรับรายละเอียด"),
    ("help.group.Work", "Work", "งาน"),
    ("help.group.Session", "Session", "เซสชัน"),
    ("help.group.Setup", "Setup", "ตั้งค่า"),
    ("help.group.project", "Project commands", "คำสั่งโปรเจกต์"),
    ("help.group.shortcuts", "Shortcuts", "ทางลัด"),
    (
        "help.copy_hint",
        "Copy: drag-select + ⌘C in your terminal (native scrollback).",
        "คัดลอก: ลากเลือกแล้ว ⌘C ในเทอร์มินัล (native scrollback)",
    ),
    ("help.glyphs.title", "Glyphs", "สัญลักษณ์"),
    ("help.usage_label", "Usage", "วิธีใช้"),
    ("help.usage", "Usage: {syntax}", "วิธีใช้: {syntax}"),
    ("help.examples", "Examples", "ตัวอย่าง"),
    ("help.related", "Related", "ที่เกี่ยวข้อง"),
    ("help.alias_of", "Alias of /{name}.", "นามแฝงของ /{name}"),
    ("help.also_aliases", "Also /{name}: {aliases}", "รวม /{name}: {aliases}"),
    (
        "help.details_hint",
        "Type /help {topic} for details.",
        "พิมพ์ /help {topic} สำหรับรายละเอียด",
    ),
    ("help.unknown", "Unknown command: /{name}", "ไม่รู้จักคำสั่ง: /{name}"),
    ("help.did_you_mean", "Did you mean", "หมายถึง"),
    ("help.list_hint", "Type /help to list commands.", "พิมพ์ /help เพื่อดูรายการคำสั่ง"),
    (
        "help.custom_note",
        "Project command — its body is sent to the agent.",
        "คำสั่งโปรเจกต์ — เนื้อหาถูกส่งให้เอเจนต์",
    ),
    (
        "help.cli_equivalent",
        "In a terminal: {command}",
        "ในเทอร์มินัล: {command}",
    ),
    ("help.mode.plan", "review only — no writes or shell", "ตรวจอย่างเดียว — ไม่เขียนไฟล์ ไม่รันเชลล์"),
    ("help.mode.ask", "confirm each change (default)", "ถามก่อนทุกการเปลี่ยน (ค่าเริ่มต้น)"),
    (
        "help.mode.auto-edit",
        "edit the workspace; shell still asks",
        "แก้ไฟล์ในเวิร์กสเปซได้; เชลล์ยังถาม",
    ),
    (
        "help.mode.yolo",
        "skip routine prompts; the danger-guard still blocks",
        "ข้ามคำถามปกติ; อันตรายยังถูกบล็อก",
    ),
    # Index one-liners — English matches CommandSpec.description (README SSOT)
    (
        "help.cmd.help.description",
        "Show commands, or details for one command.",
        "แสดงคำสั่ง หรือรายละเอียดของคำสั่งหนึ่ง",
    ),
    (
        "help.cmd.help.blurb",
        "List every built-in command, grouped by what it is for. "
        "Pass a name for syntax, examples, and related commands.",
        "แสดงคำสั่งในตัวทั้งหมด จัดกลุ่มตามหน้าที่ พิมพ์ชื่อคำสั่งเพื่อดูไวยากรณ์ ตัวอย่าง และคำสั่งที่เกี่ยวข้อง",
    ),
    (
        "help.cmd.status.description",
        "Show directory, model, mode, context, and a local recap.",
        "แสดงโฟลเดอร์ โมเดล โหมด บริบท และสรุปสั้น ๆ",
    ),
    (
        "help.cmd.status.blurb",
        "Offline session summary: where you are, which model and "
        "permission mode are active, how full the context window is, and a "
        "short recap of recent tools and files. No model call.",
        "สรุปเซสชันแบบออฟไลน์: อยู่ที่ไหน โมเดลและโหมดสิทธิ์ที่ใช้อยู่ "
        "หน้าต่างบริบทเต็มแค่ไหน และสรุปเครื่องมือกับไฟล์ล่าสุด ไม่เรียกโมเดล",
    ),
    (
        "help.cmd.model.description",
        "Show or switch the active model.",
        "แสดงหรือเปลี่ยนโมเดลที่ใช้อยู่",
    ),
    (
        "help.cmd.model.blurb",
        "With no argument, opens the model picker. `/model refresh` re-queries local endpoints.",
        "ไม่ใส่ตัวเลือกจะเปิดตัวเลือกโมเดล `/model refresh` ถาม endpoint ในเครื่องใหม่",
    ),
    (
        "help.cmd.mode.description",
        "Show or switch how much J.A.R.N. may change.",
        "แสดงหรือเปลี่ยนว่า jarn แก้ไฟล์ได้แค่ไหน",
    ),
    (
        "help.cmd.mode.blurb",
        "How much the agent may change files and run commands. Mode ids stay English.",
        "ว่าเอเจนต์แก้ไฟล์และรันคำสั่งได้แค่ไหน รหัสโหมดเป็นภาษาอังกฤษ",
    ),
    (
        "help.cmd.theme.description",
        "Show or switch the color theme.",
        "แสดงหรือเปลี่ยนชุดสี",
    ),
    (
        "help.cmd.cost.description",
        "Show session tokens and estimated cost (alias: /usage).",
        "แสดงโทเคนและค่าใช้จ่ายโดยประมาณ (นามแฝง: /usage)",
    ),
    (
        "help.cmd.cost.blurb",
        "Session spend, per-model totals, and cache reads. `/usage` is the same command.",
        "ค่าใช้จ่ายของเซสชัน รวมต่อโมเดล และการอ่านแคช `/usage` คือคำสั่งเดียวกัน",
    ),
    (
        "help.cmd.usage.description",
        "Show session tokens and estimated cost (alias for /cost).",
        "แสดงโทเคนและค่าใช้จ่ายโดยประมาณ (นามแฝงของ /cost)",
    ),
    (
        "help.cmd.context.description",
        "Show what is filling the context window.",
        "แสดงว่าอะไรกำลังกินหน้าต่างบริบท",
    ),
    (
        "help.cmd.context.blurb",
        "Visual context-window gauge plus the token size of each active "
        "prompt module. `/context all` includes inactive modules.",
        "เกจหน้าต่างบริบทและขนาดโทเคนของแต่ละส่วนเสริมที่ใช้อยู่ `/context all` รวมส่วนที่ยังไม่เปิด",
    ),
    (
        "help.cmd.verbose.description",
        "Cycle how much tool activity is shown.",
        "สลับว่าจะโชว์กิจกรรมเครื่องมือมากแค่ไหน",
    ),
    (
        "help.cmd.verbose.blurb",
        "Cycles off → new → all → verbose. "
        "off hides tool lines. new is the default (one line per tool). "
        "all includes live output tails. verbose keeps more argument detail. "
        "Session-only; persist with /config set ui.tool_progress.",
        "วน off → new → all → verbose  off ซ่อนบรรทัดเครื่องมือ  "
        "new เป็นค่าเริ่มต้น (หนึ่งบรรทัดต่อเครื่องมือ)  all รวมท้ายเอาต์พุตสด  "
        "verbose เก็บรายละเอียดอาร์กิวเมนต์มากขึ้น  เฉพาะเซสชันนี้; "
        "บันทึกด้วย /config set ui.tool_progress",
    ),
    (
        "help.cmd.focus.description",
        "Hide tool chrome and show only the answer.",
        "ซ่อนบรรทัดเครื่องมือ เหลือแค่คำตอบ",
    ),
    (
        "help.cmd.focus.blurb",
        "Display-only. Hidden tool lines are still in /expand. "
        "Turning focus on remembers your /verbose setting and restores it after.",
        "แค่การแสดงผล บรรทัดเครื่องมือที่ซ่อนยังอยู่ใน /expand  เปิด focus จะจำค่า /verbose แล้วคืนค่าเมื่อปิด",
    ),
    (
        "help.cmd.modules.description",
        "Open the prompt-module picker.",
        "เลือกส่วนเสริมของพรอมต์",
    ),
    (
        "help.cmd.modules.blurb",
        "No args opens the picker. `/modules active` prints modules "
        "currently in the assembled prompt.",
        "ไม่ใส่ตัวเลือกจะเปิดตัวเลือก `/modules active` แสดงส่วนเสริมที่อยู่ในพรอมต์ตอนนี้",
    ),
    (
        "help.cmd.module.description",
        "Activate or deactivate a prompt module.",
        "เปิดหรือปิดส่วนเสริมของพรอมต์",
    ),
    (
        "help.cmd.undo.description",
        "Revert the last agent turn's file changes.",
        "ย้อนการแก้ไฟล์ของรอบล่าสุด",
    ),
    (
        "help.cmd.redo.description",
        "Re-apply the last undone file changes.",
        "ทำซ้ำการแก้ไฟล์ที่เพิ่งย้อน",
    ),
    (
        "help.cmd.abort.description",
        "Stop this turn and roll back its file changes.",
        "หยุดรอบนี้และย้อนการแก้ไฟล์",
    ),
    (
        "help.cmd.commit.description",
        "Draft a commit from the current diff (asks first).",
        "ร่าง commit จาก diff ปัจจุบัน (ถามก่อน)",
    ),
    (
        "help.cmd.review.description",
        "Read-only review of the current diff.",
        "รีวิว diff ปัจจุบันแบบอ่านอย่างเดียว",
    ),
    (
        "help.cmd.diff.description",
        "Show a git diff of staged, working-tree, or session files.",
        "แสดง git diff ของไฟล์ staged, working tree หรือเซสชัน",
    ),
    (
        "help.cmd.diff.blurb",
        "Default: staged if the index is dirty, otherwise the working tree. "
        "`session` limits to files this thread edited.",
        "ค่าเริ่มต้น: staged ถ้า index มีการเปลี่ยนแปลง ไม่เช่นนั้น working tree  "
        "`session` จำกัดเฉพาะไฟล์ที่เธรดนี้แก้",
    ),
    (
        "help.cmd.compact.description",
        "Summarize and continue in a fresh thread.",
        "สรุปแล้วไปต่อในเธรดใหม่",
    ),
    (
        "help.cmd.compact.blurb",
        "Summarize this conversation and keep going in a new thread. "
        "`/compact status` shows whether auto-compact is on.",
        "สรุปบทสนทนานี้แล้วไปต่อในเธรดใหม่ `/compact status` แสดงว่า auto-compact เปิดอยู่หรือไม่",
    ),
    (
        "help.cmd.expand.description",
        "Show the last tool output in full.",
        "แสดงผลลัพธ์เครื่องมือล่าสุดแบบเต็ม",
    ),
    (
        "help.cmd.expand.blurb",
        "Opens the pager (same as Ctrl+O).",
        "เปิดเพจเจอร์ (เหมือน Ctrl+O)",
    ),
    (
        "help.cmd.memory.description",
        "List or edit long-term memory.",
        "ดูหรือแก้ความจำระยะยาว",
    ),
    (
        "help.cmd.clear.description",
        "Start a fresh conversation (alias: /new).",
        "เริ่มบทสนทนาใหม่ (นามแฝง: /new)",
    ),
    (
        "help.cmd.new.description",
        "Start a fresh conversation (alias for /clear).",
        "เริ่มบทสนทนาใหม่ (นามแฝงของ /clear)",
    ),
    (
        "help.cmd.config.description",
        "View or edit settings.",
        "ดูหรือแก้การตั้งค่า",
    ),
    (
        "help.cmd.config.blurb",
        "No args opens the settings panel. Changes persist to ~/.jarn/config.yaml.",
        "ไม่ใส่ตัวเลือกจะเปิดแผงตั้งค่า การเปลี่ยนแปลงถูกบันทึกที่ ~/.jarn/config.yaml",
    ),
    (
        "help.cmd.preset.description",
        "Show or apply a mode+sandbox shortcut.",
        "แสดงหรือใช้ทางลัดโหมด+sandbox",
    ),
    (
        "help.cmd.sandbox.description",
        "Show or toggle where commands run.",
        "แสดงหรือสลับว่าคำสั่งรันที่ไหน",
    ),
    (
        "help.cmd.trust.description",
        "Trust this project and lift the read-only floor.",
        "เชื่อถือโปรเจกต์นี้และยกพื้นอ่านอย่างเดียว",
    ),
    (
        "help.cmd.add-dir.description",
        "Add a directory to this session's write scope.",
        "เพิ่มโฟลเดอร์เข้าขอบเขตเขียนของเซสชันนี้",
    ),
    (
        "help.cmd.mcp.description",
        "MCP server health, prompts, and resources.",
        "สถานะเซิร์ฟเวอร์ MCP พรอมต์ และทรัพยากร",
    ),
    (
        "help.cmd.telemetry.description",
        "Show telemetry opt-in and local sink stats.",
        "แสดงการเลือก telemetry และสถิติซิงก์ในเครื่อง",
    ),
    (
        "help.cmd.skill.description",
        "Invoke a skill by name.",
        "เรียกสกิลตามชื่อ",
    ),
    (
        "help.cmd.skill.blurb",
        "Injects the skill body into this turn. Installed skills are also "
        "slash commands: `/skill-name` works like `/skill skill-name`.",
        "ฉีดเนื้อหาสกิลเข้าเทิร์นนี้ สกิลที่ติดตั้งเป็นคำสั่งสแลชได้ด้วย: `/skill-name` ใช้ได้เหมือน `/skill skill-name`",
    ),
    (
        "help.cmd.skills.description",
        "List available skills.",
        "รายการสกิลที่มี",
    ),
    (
        "help.cmd.init.description",
        "Create a JARN.md project context file.",
        "สร้างไฟล์บริบทโปรเจกต์ JARN.md",
    ),
    (
        "help.cmd.permissions.description",
        "Show permission rules and the allowlist.",
        "แสดงกฎสิทธิ์และรายการที่อนุญาต",
    ),
    (
        "help.cmd.key.description",
        "Set the API key for the current provider (keychain).",
        "ตั้ง API key ของผู้ให้บริการปัจจุบัน (keychain)",
    ),
    (
        "help.cmd.login.description",
        "Sign in to ChatGPT.",
        "เข้าสู่ระบบ ChatGPT",
    ),
    (
        "help.cmd.login.blurb",
        "Sign in to ChatGPT. Reports success only after the account is verified.",
        "เข้าสู่ระบบ ChatGPT สำเร็จเมื่อบัญชีถูกยืนยันแล้ว",
    ),
    (
        "help.cmd.logout.description",
        "Sign out of ChatGPT.",
        "ออกจากระบบ ChatGPT",
    ),
    (
        "help.cmd.logout.blurb",
        "Sign out of ChatGPT. Removes only Codex-managed ChatGPT "
        "credentials; provider API keys are kept.",
        "ออกจากระบบ ChatGPT ลบเฉพาะข้อมูลรับรอง ChatGPT ที่ Codex จัดการ คีย์ API ของผู้ให้บริการยังอยู่",
    ),
    (
        "help.cmd.doctor.description",
        "Diagnose configuration, providers, and keys.",
        "ตรวจการตั้งค่า ผู้ให้บริการ และคีย์",
    ),
    (
        "help.cmd.tools.description",
        "List tools the agent can use this session.",
        "รายการเครื่องมือที่เอเจนต์ใช้ได้ในเซสชันนี้",
    ),
    (
        "help.cmd.sessions.description",
        "Pick a previous session, or list them (alias: /resume).",
        "เลือกเซสชันก่อนหน้า หรือแสดงรายการ (นามแฝง: /resume)",
    ),
    (
        "help.cmd.sessions.blurb",
        "In the REPL, opens the session picker. Pass a query to filter "
        "by title or id. Non-TTY callers get a text list.",
        "ใน REPL เปิดตัวเลือกเซสชัน ใส่คำค้นเพื่อกรองชื่อหรือ id ผู้เรียกที่ไม่ใช่ TTY ได้รายการข้อความ",
    ),
    (
        "help.cmd.resume.description",
        "Pick a previous session to resume (alias for /sessions).",
        "เลือกเซสชันก่อนหน้าเพื่อทำต่อ (นามแฝงของ /sessions)",
    ),
    (
        "help.cmd.rewind.description",
        "Rewind to an earlier turn (forks a new thread).",
        "ย้อนไปเทิร์นก่อนหน้า (แยกเธรดใหม่)",
    ),
    (
        "help.cmd.rewind.blurb",
        "Fork to an earlier turn and continue. Optionally restore files "
        "to that turn's checkpoint too.",
        "แยกไปเทิร์นก่อนหน้าแล้วไปต่อ จะกู้ไฟล์จากจุดเซฟของเทิร์นนั้นด้วยก็ได้",
    ),
    (
        "help.cmd.title.description",
        "Show or set this session's title.",
        "แสดงหรือตั้งชื่อเซสชันนี้",
    ),
    (
        "help.cmd.checkpoints.description",
        "List recent auto-checkpoints.",
        "รายการจุดเซฟอัตโนมัติล่าสุด",
    ),
    (
        "help.cmd.ps.description",
        "List or kill background processes.",
        "แสดงหรือฆ่าโปรเซสพื้นหลัง",
    ),
    (
        "help.cmd.queue.description",
        "Show or manage queued input lines.",
        "แสดงหรือจัดการบรรทัดที่รอคิว",
    ),
    (
        "help.cmd.busy.description",
        "Set what Enter does while a turn is running.",
        "ตั้งว่า Enter ทำอะไรขณะรอบกำลังรัน",
    ),
    (
        "help.cmd.busy.blurb",
        "Session-only. queue (default) holds the line until the turn "
        "ends. steer injects via the existing steer slot (needs ui.steering). "
        "interrupt aborts then runs the line. Persist with "
        "/config set ui.busy_input_mode.",
        "เฉพาะเซสชัน queue (ค่าเริ่มต้น) เก็บบรรทัดไว้จนรอบจบ  "
        "steer ฉีดผ่านช่อง steer ที่มีอยู่ (ต้องมี ui.steering)  "
        "interrupt ยกเลิกแล้วรันบรรทัด  บันทึกด้วย /config set ui.busy_input_mode",
    ),
    (
        "help.cmd.map.description",
        "Show a map of this repository.",
        "แสดงแผนที่ของรีโปนี้",
    ),
    (
        "help.cmd.wiki.description",
        "Search or list wiki pages.",
        "ค้นหาหรือรายการหน้าวิกิ",
    ),
    (
        "help.cmd.quit.description",
        "Exit J.A.R.N. (alias: /exit).",
        "ออกจาก J.A.R.N. (นามแฝง: /exit)",
    ),
    (
        "help.cmd.exit.description",
        "Exit J.A.R.N. (alias for /quit).",
        "ออกจาก J.A.R.N. (นามแฝงของ /quit)",
    ),
    # S10 — jarn doctor headings / pass-fail (JSON keys stay English)
    ("doctor.title", "jarn doctor", "jarn doctor"),
    ("doctor.section.providers", "Providers", "ผู้ให้บริการ"),
    ("doctor.section.extensions", "Extensions", "ส่วนขยาย"),
    ("doctor.section.errors", "Errors", "ข้อผิดพลาด"),
    ("doctor.section.skills", "Skills", "ทักษะ"),
    ("doctor.section.commands", "Commands", "คำสั่ง"),
    ("doctor.section.subagents", "Subagents", "เอเจนต์ย่อย"),
    ("doctor.section.hooks", "Hooks", "Hooks"),
    ("doctor.section.mcp", "MCP", "MCP"),
    ("doctor.section.shadowed", "Shadowed", "ถูกบัง"),
    ("doctor.status.ok", "ok", "ผ่าน"),
    ("doctor.status.fail", "fail", "ไม่ผ่าน"),
    ("doctor.status.missing", "missing", "ไม่มี"),
    ("doctor.status.found", "found", "พบ"),
    ("doctor.status.unavailable", "unavailable", "ใช้ไม่ได้"),
    ("doctor.status.available", "available", "พร้อม"),
    ("doctor.status.key_ok", "key ok", "คีย์พร้อม"),
    ("doctor.status.on", "on", "เปิด"),
    ("doctor.status.off", "off", "ปิด"),
    ("doctor.status.present", "present", "มี"),
    ("doctor.status.writable", "writable", "เขียนได้"),
    ("doctor.status.not_writable", "not writable", "เขียนไม่ได้"),
    ("doctor.status.secure", "secure", "ปลอดภัย"),
    ("doctor.status.compatible", "compatible", "เข้ากันได้"),
    (
        "doctor.status.incompatible",
        "incompatible or unavailable",
        "เข้ากันไม่ได้หรือใช้ไม่ได้",
    ),
    ("doctor.status.enabled", "enabled", "เปิด"),
    ("doctor.status.disabled", "disabled", "ปิด"),
    ("doctor.status.none", "none", "ไม่มี"),
    ("doctor.status.unknown", "unknown", "ไม่ทราบ"),
    ("doctor.status.not_on_path", "not on PATH", "ไม่อยู่บน PATH"),
    ("doctor.status.version_unknown", "version unknown", "ไม่ทราบเวอร์ชัน"),
    ("doctor.cta.ok", "All good.", "ผ่านหมด"),
    (
        "doctor.cta.issues",
        "{n} issues — see above. Run jarn doctor --fix --dry-run to preview repairs.",
        "{n} ปัญหา — ดูด้านบน รัน jarn doctor --fix --dry-run เพื่อดูการซ่อม",
    ),
    ("doctor.no_config", "No config — run jarn setup.", "ยังไม่มี config — รัน jarn setup"),
    ("doctor.label.version", "Version", "เวอร์ชัน"),
    ("doctor.label.executable", "Executable", "ตัวรัน"),
    ("doctor.label.host", "Host", "โฮสต์"),
    ("doctor.label.shell", "Shell", "เชลล์"),
    ("doctor.label.install_dir", "Install dir", "โฟลเดอร์ติดตั้ง"),
    ("doctor.label.protocol", "Protocol", "โปรโตคอล"),
    ("doctor.label.global_schema", "Global schema", "สคีมาโกลบอล"),
    ("doctor.label.project_schema", "Project schema", "สคีมาโปรเจกต์"),
    ("doctor.label.secrets", "Secrets", "ความลับ"),
    ("doctor.label.catalog", "Catalog", "แคตตาล็อก"),
    ("doctor.label.updates", "Updates", "อัปเดต"),
    ("doctor.label.network", "Network", "เครือข่าย"),
    ("doctor.label.config", "Config", "Config"),
    ("doctor.label.project", "Project", "โปรเจกต์"),
    ("doctor.label.extra_roots", "Extra roots", "รูทเพิ่ม"),
    ("doctor.label.profile", "Profile", "โปรไฟล์"),
    ("doctor.label.model", "Model", "โมเดล"),
    ("doctor.label.mode", "Mode", "โหมด"),
    ("doctor.label.web", "Web tools", "เว็บทูล"),
    ("doctor.label.sandbox", "Sandbox", "แซนด์บ็อกซ์"),
    ("doctor.label.execution", "Execution", "การรัน"),
    ("doctor.label.git", "Git", "Git"),
    ("doctor.label.wiki", "Wiki", "Wiki"),
    ("doctor.label.transcript", "Transcript", "บันทึก"),
    ("doctor.label.repo_map", "Repo map", "แผนที่รีโป"),
    ("doctor.label.modules", "Modules", "โมดูล"),
    (
        "doctor.untrusted",
        "project untrusted — stripped keys: {keys}",
        "โปรเจกต์ไม่น่าเชื่อถือ — คีย์ที่ตัดออก: {keys}",
    ),
    (
        "doctor.untrusted.hint",
        "(run `jarn trust <root>` to enable)",
        "(รัน `jarn trust <root>` เพื่อเปิดใช้)",
    ),
    (
        "doctor.extra_roots.hint",
        "(write scope only — checkpoint/context stay primary)",
        "(เขียนได้อย่างเดียว — checkpoint/context ยังอยู่ที่รูทหลัก)",
    ),
    (
        "doctor.off_path",
        "other installation (off PATH)",
        "การติดตั้งอื่น (นอก PATH)",
    ),
    ("doctor.shadowing", "shadowing candidate", "ตัวรันบัง"),
    ("doctor.install_invalid", "install record invalid", "บันทึกติดตั้งใช้ไม่ได้"),
    ("doctor.activation_mismatch", "activation mismatch", "การเปิดใช้ไม่ตรง"),
    (
        "doctor.activation_mismatch.detail",
        "active executable differs from metadata",
        "ตัวรันที่ใช้อยู่ไม่ตรงกับ metadata",
    ),
    (
        "doctor.ext.untrusted",
        "project untrusted — project-tier files/config skipped",
        "โปรเจกต์ไม่น่าเชื่อถือ — ข้ามไฟล์/config ระดับโปรเจกต์",
    ),
    (
        "doctor.ext.counts",
        "skills {skills} · commands {commands} · subagents {subagents} · "
        "hooks {hooks} · mcp {mcp} · async {async_n}",
        "ทักษะ {skills} · คำสั่ง {commands} · เอเจนต์ย่อย {subagents} · "
        "ฮุก {hooks} · mcp {mcp} · async {async_n}",
    ),
    ("doctor.hook.blocking", "blocking", "บล็อก"),
    ("doctor.hook.nonblocking", "non-blocking", "ไม่บล็อก"),
    ("doctor.secrets.issues", "{n} permission issue(s)", "{n} ปัญหาสิทธิ์"),
    ("doctor.free_bytes", "{n} GiB free", "{n} GiB ว่าง"),
    ("doctor.free_unknown", "free space unknown", "ไม่ทราบพื้นที่ว่าง"),
    (
        "doctor.network.summary",
        "checked {total} · reachable {reachable}",
        "ตรวจ {total} · ถึง {reachable}",
    ),
    (
        "doctor.modules.summary",
        "{n} active · {tokens} tok assembled",
        "{n} ใช้งาน · {tokens} tok ที่ประกอบ",
    ),
    (
        "doctor.mode.clamped",
        "{mode} · effective: {effective} (after trust clamp)",
        "{mode} · ที่ใช้จริง: {effective} (หลังจำกัดจาก trust)",
    ),
    (
        "doctor.updates.summary",
        "{channel} channel · checks {checks}",
        "ช่อง {channel} · ตรวจ {checks}",
    ),
    (
        "doctor.catalog.summary",
        "{source} · {freshness}",
        "{source} · {freshness}",
    ),
    (
        "doctor.errors.next_default",
        "Run jarn doctor again.",
        "รัน jarn doctor อีกครั้ง",
    ),
    ("doctor.errors.check_failed", "Check failed.", "ตรวจไม่ผ่าน"),
    ("doctor.modules.unavailable", "prompt module diagnostics unavailable", "ตรวจโมดูลพรอมต์ไม่ได้"),
    # S11 — onboarding chrome (provider / model ids stay English)
    (
        "onboarding.intro",
        "Let's get you set up. This writes {path}.",
        "มาตั้งค่ากัน ไฟล์ที่จะเขียนคือ {path}",
    ),
    (
        "onboarding.overwrite",
        "{path} exists. Update setup selections? (advanced settings will be preserved)",
        "{path} มีอยู่แล้ว อัปเดตตัวเลือก setup? (การตั้งค่าขั้นสูงจะถูกเก็บไว้)",
    ),
    ("onboarding.overwrite.keep", "Keeping existing config.", "คงไฟล์ตั้งค่าเดิมไว้"),
    (
        "onboarding.resume",
        "Resume setup from {stage} (saved {updated_at})?",
        "ทำ setup ต่อจาก {stage} (บันทึกเมื่อ {updated_at})?",
    ),
    ("onboarding.resuming", "Resuming at {stage}.", "ทำต่อที่ {stage}"),
    ("onboarding.cancelled", "Setup incomplete (cancelled).", "Setup ไม่สมบูรณ์ (ยกเลิก)"),
    ("onboarding.resume_with", "Resume with {command}.", "ทำต่อด้วย {command}"),
    (
        "onboarding.incomplete.install",
        "Setup incomplete (install state):",
        "Setup ไม่สมบูรณ์ (สถานะการติดตั้ง):",
    ),
    ("onboarding.incomplete.at", "Setup incomplete at {stage}:", "Setup ไม่สมบูรณ์ที่ {stage}:"),
    (
        "onboarding.retry",
        "No configuration was changed. Retry with {command}.",
        "ยังไม่ได้เปลี่ยนไฟล์ตั้งค่า ลองใหม่ด้วย {command}",
    ),
    ("onboarding.incomplete", "Setup incomplete:", "Setup ไม่สมบูรณ์:"),
    (
        "onboarding.progress_saved",
        "Your progress is saved. Resume with {command}.",
        "ความคืบหน้าถูกบันทึกไว้ ทำต่อด้วย {command}",
    ),
    ("onboarding.unconfirmed", "no configuration was confirmed.", "ยังไม่ได้ยืนยันการตั้งค่า"),
    (
        "onboarding.incomplete.verification",
        "Setup incomplete at verification:",
        "Setup ไม่สมบูรณ์ตอนตรวจสอบ:",
    ),
    ("onboarding.terminal_closed", "terminal closed", "ปิดเทอร์มินัลแล้ว"),
    ("onboarding.nav.back_cancel", "(type /back or /cancel)", "(พิมพ์ /back หรือ /cancel)"),
    (
        "onboarding.tui.help",
        "↑/↓ select · Enter confirm · Esc back",
        "↑/↓ เลือก · Enter ยืนยัน · Esc ย้อนกลับ",
    ),
    ("onboarding.connect.heading", "Choose how to connect:", "เลือกวิธีเชื่อมต่อ:"),
    ("onboarding.connect.prompt", "How do you want to connect?", "ต้องการเชื่อมต่อแบบไหน?"),
    ("onboarding.connect.chatgpt", "Continue with ChatGPT", "ใช้ ChatGPT ต่อ"),
    (
        "onboarding.connect.chatgpt.detail",
        "Continue with ChatGPT  (subscription; no API key)",
        "ใช้ ChatGPT ต่อ  (สมัครสมาชิก ไม่ต้องใช้ API key)",
    ),
    ("onboarding.connect.anthropic", "Use Anthropic", "ใช้ Anthropic"),
    ("onboarding.connect.opencode", "Use OpenCode Go", "ใช้ OpenCode Go"),
    ("onboarding.connect.cloud", "Use another cloud provider", "ใช้ผู้ให้บริการคลาวด์รายอื่น"),
    ("onboarding.connect.local", "Use a local model", "ใช้โมเดลในเครื่อง"),
    ("onboarding.connect.local.found", " — found {names}", " — พบ {names}"),
    ("onboarding.connect.local.found_paren", "(found {names})", "(พบ {names})"),
    (
        "onboarding.connect.advanced.hint",
        "Advanced: choose 'advanced' for custom endpoints and the full registry.",
        "ขั้นสูง: พิมพ์ advanced สำหรับเอนด์พอยต์กำหนดเองและรายชื่อผู้ให้บริการทั้งหมด",
    ),
    (
        "onboarding.connect.advanced",
        "Advanced  (custom endpoints and full provider list)",
        "ขั้นสูง  (เอนด์พอยต์กำหนดเองและรายชื่อผู้ให้บริการทั้งหมด)",
    ),
    (
        "onboarding.recommended.signed_in",
        "★ recommended — already signed in",
        "★ แนะนำ — ลงชื่อเข้าแล้ว",
    ),
    ("onboarding.recommended.key", "★ recommended — key found", "★ แนะนำ — พบคีย์แล้ว"),
    ("onboarding.cloud.prompt", "Cloud provider", "ผู้ให้บริการคลาวด์"),
    ("onboarding.local.prompt", "Local provider", "ผู้ให้บริการในเครื่อง"),
    ("onboarding.provider.prompt", "Provider", "ผู้ให้บริการ"),
    ("onboarding.advanced.list", "Advanced providers: {names}", "ผู้ให้บริการขั้นสูง: {names}"),
    ("onboarding.cloud.choose", "Choose another cloud provider", "เลือกผู้ให้บริการคลาวด์รายอื่น"),
    ("onboarding.local.choose", "Choose a local model server", "เลือกเซิร์ฟเวอร์โมเดลในเครื่อง"),
    ("onboarding.advanced.choose", "Advanced provider selection", "เลือกผู้ให้บริการแบบขั้นสูง"),
    (
        "onboarding.storage.prompt",
        "How should J.A.R.N. read the {provider} API key?",
        "ให้ J.A.R.N. อ่าน API key ของ {provider} จากไหน?",
    ),
    (
        "onboarding.storage.prompt_your",
        "How should J.A.R.N. read your {provider} API key?",
        "ให้ J.A.R.N. อ่าน API key ของ {provider} จากไหน?",
    ),
    (
        "onboarding.storage.env.using",
        "Using {env_var} from your environment.",
        "ใช้ {env_var} จากสภาพแวดล้อม",
    ),
    ("onboarding.storage.env.unset", "{env_var} is not set.", "{env_var} ยังไม่ได้ตั้ง"),
    (
        "onboarding.storage.env.paste_now",
        "Paste the key now; only its secure-store reference will be saved.",
        "วางคีย์ตอนนี้ จะบันทึกเฉพาะการอ้างอิงที่เก็บอย่างปลอดภัย",
    ),
    ("onboarding.storage.env.read", "Read from {env_var}", "อ่านจาก {env_var}"),
    (
        "onboarding.storage.env.found",
        "Read from {env_var} (found in your environment)",
        "อ่านจาก {env_var} (พบในสภาพแวดล้อมแล้ว)",
    ),
    (
        "onboarding.storage.env.set_before",
        "Read from {env_var} — set it before launching",
        "อ่านจาก {env_var} — ตั้งค่าก่อนเปิดใช้งาน",
    ),
    (
        "onboarding.storage.env.recommended",
        "Read from an environment variable (recommended)",
        "อ่านจากตัวแปรสภาพแวดล้อม (แนะนำ)",
    ),
    (
        "onboarding.storage.keychain",
        "Paste it now → store in the OS keychain",
        "วางตอนนี้ → เก็บใน keychain ของระบบ",
    ),
    (
        "onboarding.storage.oauth",
        "Log in with browser (recommended)",
        "ลงชื่อเข้าด้วยเบราว์เซอร์ (แนะนำ)",
    ),
    ("onboarding.storage.method", "storage", "ที่เก็บคีย์"),
    ("onboarding.key.paste", "Paste the {provider} API key", "วาง API key ของ {provider}"),
    ("onboarding.key.paste_prompt", "paste API key", "วาง API key"),
    (
        "onboarding.key.required",
        "A key is required to finish this provider setup.",
        "ต้องมีคีย์ถึงจะตั้งค่าผู้ให้บริการนี้ได้",
    ),
    (
        "onboarding.key.paste.tui",
        "Paste your API key (stored in the OS keychain)",
        "วาง API key (จะเก็บใน keychain ของระบบ)",
    ),
    (
        "onboarding.key.env_missing",
        "{env_var} is not set — paste your {provider} API key now (stored in the OS keychain)",
        "{env_var} ยังไม่ได้ตั้ง — วาง API key ของ {provider} ตอนนี้ (จะเก็บใน keychain ของระบบ)",
    ),
    (
        "onboarding.key.codex",
        "codex_subscription uses Codex-managed ChatGPT login — no API key.",
        "codex_subscription ใช้การลงชื่อเข้า ChatGPT ผ่าน Codex — ไม่ต้องใช้ API key",
    ),
    (
        "onboarding.key.local",
        "{provider} is local — no API key needed.",
        "{provider} อยู่บนเครื่องนี้ — ไม่ต้องใช้ API key",
    ),
    (
        "onboarding.key.read_from",
        "J.A.R.N. will read the key from {env_var}.",
        "J.A.R.N. จะอ่านคีย์จาก {env_var}",
    ),
    (
        "onboarding.key.read_it",
        "J.A.R.N. will read it from {env_var}.",
        "J.A.R.N. จะอ่านจาก {env_var}",
    ),
    (
        "onboarding.key.stored",
        "stored in OS keychain (jarn/{provider})",
        "เก็บใน keychain ของระบบ (jarn/{provider})",
    ),
    (
        "onboarding.oauth.opening",
        "Opening your browser for OpenRouter login…",
        "กำลังเปิดเบราว์เซอร์เพื่อลงชื่อเข้า OpenRouter…",
    ),
    (
        "onboarding.oauth.logged_in",
        "Logged in — key stored as {reference}",
        "ลงชื่อเข้าแล้ว — เก็บคีย์เป็น {reference}",
    ),
    (
        "onboarding.oauth.failed",
        "Browser login failed: {error}. Falling back to manual key entry.",
        "ลงชื่อเข้าด้วยเบราว์เซอร์ไม่สำเร็จ: {error} จะให้วางคีย์เองแทน",
    ),
    ("onboarding.base_url.prompt", "API base URL for {provider}", "API base URL ของ {provider}"),
    (
        "onboarding.base_url.hint.ollama",
        "Ollama host URL (no /v1 suffix)",
        "URL ของโฮสต์ Ollama (ไม่ต้องมี /v1)",
    ),
    (
        "onboarding.base_url.hint.compat",
        "bare host → /v1 appended",
        "โฮสต์เปล่า → จะต่อ /v1 ให้",
    ),
    ("onboarding.base_url.hint.other", "include /v1 when required", "ใส่ /v1 เมื่อจำเป็น"),
    (
        "onboarding.model.using_local",
        "Using model {model} reported by the local server.",
        "ใช้โมเดล {model} ที่เซิร์ฟเวอร์ในเครื่องรายงานมา",
    ),
    (
        "onboarding.model.start_or_manual",
        "Start/download a model, enter one manually, or go back.",
        "เปิดหรือดาวน์โหลดโมเดล กรอกเอง หรือย้อนกลับ",
    ),
    (
        "onboarding.model.choose_reported",
        "Choose a reported model or enter one manually.",
        "เลือกโมเดลที่รายงานมา หรือกรอกเอง",
    ),
    (
        "onboarding.model.manual_gate",
        "Advanced manual entry is allowed, but must pass final validation.",
        "กรอกเองได้ในโหมดขั้นสูง แต่ต้องผ่านการตรวจสอบตอนท้าย",
    ),
    ("onboarding.model.prompt", "Model for {provider}", "โมเดลของ {provider}"),
    ("onboarding.model.id", "Model id for {provider}", "รหัสโมเดลของ {provider}"),
    ("onboarding.model.id.endpoint", "Model id on your endpoint", "รหัสโมเดลบนเอนด์พอยต์ของคุณ"),
    (
        "onboarding.model.id.example",
        "Model id for {provider}  (e.g. deepseek/deepseek-v4-flash for OpenRouter)",
        "รหัสโมเดลของ {provider}  (เช่น deepseek/deepseek-v4-flash สำหรับ OpenRouter)",
    ),
    (
        "onboarding.model.id.compat",
        "Model id on your endpoint  (e.g. gpt-4o, qwen3-coder)",
        "รหัสโมเดลบนเอนด์พอยต์ของคุณ  (เช่น gpt-4o, qwen3-coder)",
    ),
    ("onboarding.model.manual", "Enter a model id manually…", "กรอกรหัสโมเดลเอง…"),
    (
        "onboarding.model.pick",
        "Pick a model reported by {provider}  ({n} found; {status})",
        "เลือกโมเดลที่ {provider} รายงานมา  (พบ {n}; {status})",
    ),
    (
        "onboarding.model.checking",
        "Checking models reported by {provider}…",
        "กำลังตรวจโมเดลที่ {provider} รายงานมา…",
    ),
    (
        "onboarding.model.checking.detail",
        "This is a read-only catalog request. It will stop at the configured catalog timeout.",
        "นี่เป็นคำขอแค็ตตาล็อกแบบอ่านอย่างเดียว จะหยุดเมื่อครบเวลาที่ตั้งไว้",
    ),
    (
        "onboarding.model.catalog_unreachable",
        "couldn't reach or verify the catalog at {endpoint} — {status}",
        "เชื่อมต่อหรือตรวจแค็ตตาล็อกที่ {endpoint} ไม่ได้ — {status}",
    ),
    (
        "onboarding.model.enter_unverified",
        "Enter a model id for {provider} manually (unverified)",
        "กรอกรหัสโมเดลของ {provider} เอง (ยังไม่ตรวจ)",
    ),
    (
        "onboarding.model.manual_unverified",
        "Model id for {provider} (manual, unverified) — {hint}",
        "รหัสโมเดลของ {provider} (กรอกเอง ยังไม่ตรวจ) — {hint}",
    ),
    (
        "onboarding.model.manual_notice",
        "Manual entry; availability must pass the final readiness gate",
        "กรอกเอง ความพร้อมต้องผ่านการตรวจสอบตอนท้าย",
    ),
    ("onboarding.model.main", "Default model (main agent)", "โมเดลหลัก (เอเจนต์หลัก)"),
    ("onboarding.model.account_default", "account default", "ค่าเริ่มต้นของบัญชี"),
    (
        "onboarding.model.account_default_checked",
        "account default (checked before save)",
        "ค่าเริ่มต้นของบัญชี (จะตรวจก่อนบันทึก)",
    ),
    ("onboarding.reasoning", "Reasoning effort", "ระดับการคิด"),
    ("onboarding.reasoning.default", "Provider/model default", "ค่าเริ่มต้นของผู้ให้บริการ/โมเดล"),
    ("onboarding.reasoning.low", "Low", "ต่ำ"),
    ("onboarding.reasoning.medium", "Medium", "กลาง"),
    ("onboarding.reasoning.high", "High", "สูง"),
    ("onboarding.reasoning.xhigh", "Extra high", "สูงพิเศษ"),
    (
        "onboarding.subagent",
        "Subagent model (use profile/model for another provider)",
        "โมเดลซับเอเจนต์ (ใช้ profile/model หากเป็นผู้ให้บริการอื่น)",
    ),
    (
        "onboarding.summarizer",
        "Summarizer model (use profile/model for another provider)",
        "โมเดลสรุป (ใช้ profile/model หากเป็นผู้ให้บริการอื่น)",
    ),
    (
        "onboarding.fallback",
        "Fallback models, comma-separated (blank for none)",
        "โมเดลสำรอง คั่นด้วยจุลภาค (ว่างได้)",
    ),
    ("onboarding.budget", "Maximum cost per session in USD", "เพดานค่าใช้จ่ายต่อเซสชัน (USD)"),
    (
        "onboarding.budget.invalid",
        "Enter a finite number greater than or equal to 0.",
        "กรอกตัวเลขจำกัดที่มากกว่าหรือเท่ากับ 0",
    ),
    (
        "onboarding.budget.warn",
        "Warn when this percentage of the budget is used",
        "เตือนเมื่อใช้งบถึงเปอร์เซ็นต์นี้",
    ),
    (
        "onboarding.budget.warn.invalid",
        "Enter a whole number from 0 through 100.",
        "กรอกจำนวนเต็มตั้งแต่ 0 ถึง 100",
    ),
    (
        "onboarding.budget.stop",
        "Stop automatically at the session budget?",
        "หยุดอัตโนมัติเมื่อถึงงบของเซสชัน?",
    ),
    (
        "onboarding.budget.stop.when",
        "When the session budget is reached",
        "เมื่อถึงงบของเซสชัน",
    ),
    ("onboarding.budget.stop.auto", "Stop automatically", "หยุดอัตโนมัติ"),
    ("onboarding.budget.stop.warn_only", "Warn only", "เตือนอย่างเดียว"),
    ("onboarding.perm.prompt", "Permission profile", "โปรไฟล์สิทธิ์"),
    ("onboarding.perm.review", "read and plan only", "อ่านและวางแผนอย่างเดียว"),
    ("onboarding.perm.ask", "ask before changes (recommended)", "ถามก่อนเปลี่ยน (แนะนำ)"),
    (
        "onboarding.perm.edit",
        "edit workspace; ask before commands/external actions",
        "แก้ไฟล์ใน workspace; ถามก่อนรันคำสั่งหรือการกระทำภายนอก",
    ),
    (
        "onboarding.perm.full",
        "skip routine prompts; hard safety blocks remain",
        "ข้ามคำถามประจำ; บล็อกความปลอดภัยยังอยู่",
    ),
    ("onboarding.perm.tui.plan", "Review only — read and plan", "ตรวจอย่างเดียว — อ่านและวางแผน"),
    (
        "onboarding.perm.tui.ask",
        "Ask before changes — recommended",
        "ถามก่อนเปลี่ยน — แนะนำ",
    ),
    (
        "onboarding.perm.tui.edit",
        "Edit workspace; ask before commands and external actions",
        "แก้ไฟล์ใน workspace; ถามก่อนรันคำสั่งและการกระทำภายนอก",
    ),
    (
        "onboarding.perm.tui.yolo",
        "Full access; hard safety blocks remain",
        "เข้าถึงเต็มที่; บล็อกความปลอดภัยยังอยู่",
    ),
    ("onboarding.theme", "Theme", "ธีม"),
    ("onboarding.theme.prompt", "Theme?", "ธีม?"),
    ("onboarding.theme.dark", "Dark", "มืด"),
    ("onboarding.theme.light", "Light", "สว่าง"),
    ("onboarding.theme.high_contrast", "High contrast", "คอนทราสต์สูง"),
    ("onboarding.confirm.ready", "Ready to finish:", "พร้อมบันทึก:"),
    ("onboarding.confirm.ready_q", "Ready?", "พร้อมแล้ว?"),
    ("onboarding.confirm.finish", "Finish setup?", "จบการตั้งค่า?"),
    ("onboarding.confirm.save", "Save configuration", "บันทึกการตั้งค่า"),
    ("onboarding.confirm.back", "Go back", "ย้อนกลับ"),
    ("onboarding.field.provider", "Provider", "ผู้ให้บริการ"),
    ("onboarding.field.model", "Model", "โมเดล"),
    ("onboarding.field.catalog", "Catalog", "แค็ตตาล็อก"),
    ("onboarding.field.theme", "Theme", "ธีม"),
    ("onboarding.field.subagent", "Subagent", "ซับเอเจนต์"),
    ("onboarding.field.summarizer", "Summarizer", "ตัวสรุป"),
    ("onboarding.field.fallbacks", "Fallbacks", "โมเดลสำรอง"),
    ("onboarding.field.budget", "Budget", "งบ"),
    ("onboarding.field.permissions", "Permissions", "สิทธิ์"),
    ("onboarding.field.key", "key", "คีย์"),
    ("onboarding.field.base_url", "base_url", "base_url"),
    ("onboarding.field.reasoning", "reasoning", "การคิด"),
    ("onboarding.field.access", "access", "สิทธิ์เข้าถึง"),
    ("onboarding.catalog.verified", "verified", "ตรวจแล้ว"),
    ("onboarding.catalog.unverified", "unverified", "ยังไม่ตรวจ"),
    ("onboarding.none", "(none)", "(ไม่มี)"),
    ("onboarding.key.managed_codex", "(managed by Codex)", "(จัดการโดย Codex)"),
    ("onboarding.key.none_local", "(none — local)", "(ไม่มี — ในเครื่อง)"),
    (
        "onboarding.key.pasted",
        "(pasted; held in memory until verified commit)",
        "(วางแล้ว เก็บในหน่วยความจำจนกว่าจะบันทึกหลังตรวจ)",
    ),
    (
        "onboarding.budget.summary",
        "{amount} (warn {pct}%, hard stop {hard})",
        "{amount} (เตือน {pct}% หยุดทันที {hard})",
    ),
    ("onboarding.reasoning.provider_default", "provider default", "ค่าเริ่มต้นของผู้ให้บริการ"),
    (
        "onboarding.validate.required",
        "Required readiness validation (may be billable)",
        "ต้องตรวจความพร้อม (อาจคิดเงิน)",
    ),
    (
        "onboarding.validate.credits",
        "sends one real model request and may consume provider credits.",
        "จะส่งคำขอโมเดลจริงหนึ่งครั้ง และอาจใช้เครดิตของผู้ให้บริการ",
    ),
    (
        "onboarding.validate.confirm",
        "Send the validation request and finish setup?",
        "ส่งคำขอตรวจแล้วจบการตั้งค่า?",
    ),
    (
        "onboarding.validate.using_default",
        "Using provider-reported default {name}",
        "ใช้ค่าเริ่มต้นที่ผู้ให้บริการรายงาน {name}",
    ),
    (
        "onboarding.validate.status",
        "validating — the model may need to load first (can take ~1 min); Ctrl+C to skip",
        "กำลังตรวจ — โมเดลอาจต้องโหลดก่อน (ราว 1 นาที); Ctrl+C เพื่อข้าม",
    ),
    ("onboarding.validate.ok", "model responded ({n} chars)", "โมเดลตอบแล้ว ({n} ตัวอักษร)"),
    (
        "onboarding.validate.skipped",
        "skipped — the isolated validation worker was stopped; retry setup when ready.",
        "ข้ามแล้ว — ตัวตรวจแยกถูกหยุด ลอง setup ใหม่เมื่อพร้อม",
    ),
    (
        "onboarding.validate.timeout",
        "validation timed out after {seconds}s — the isolated request was stopped and can be retried safely.",
        "การตรวจหมดเวลาหลัง {seconds} วินาที — คำขอแยกถูกหยุดแล้ว ลองใหม่ได้",
    ),
    (
        "onboarding.validate.adjust",
        "Adjust the key/model later in ~/.jarn/config.yaml if needed.",
        "แก้คีย์หรือโมเดลทีหลังใน ~/.jarn/config.yaml ได้",
    ),
    ("onboarding.validate.failed", "validation failed: {error}", "การตรวจไม่สำเร็จ: {error}"),
    (
        "onboarding.validate.fix_later",
        "You can fix the key/model later in ~/.jarn/config.yaml.",
        "แก้คีย์หรือโมเดลทีหลังใน ~/.jarn/config.yaml ได้",
    ),
    ("onboarding.complete.banner", "Setup complete.", "ตั้งค่าเสร็จแล้ว"),
    ("onboarding.complete.executable", "Executable", "ไฟล์รัน"),
    ("onboarding.complete.config", "Config", "ไฟล์ตั้งค่า"),
    ("onboarding.complete.backup", "Previous config backup", "สำเนาไฟล์ตั้งค่าเดิม"),
    ("onboarding.complete.auth", "Authentication", "การยืนยันตัวตน"),
    ("onboarding.complete.chatgpt_plan", "ChatGPT plan", "แผน ChatGPT"),
    ("onboarding.complete.workspace", "Workspace", "Workspace"),
    ("onboarding.complete.reasoning", "Reasoning", "การคิด"),
    ("onboarding.complete.permission", "Permission", "สิทธิ์"),
    ("onboarding.complete.cwd", "Working directory", "ไดเรกทอรีที่ทำงาน"),
    ("onboarding.complete.validation", "Provider validation", "การตรวจผู้ให้บริการ"),
    ("onboarding.complete.next", "Next command", "คำสั่งถัดไป"),
    ("onboarding.complete.verified", "verified", "ตรวจแล้ว"),
    ("onboarding.complete.unverified", "unverified", "ยังไม่ตรวจ"),
    ("onboarding.complete.chatgpt_sub", "ChatGPT subscription", "สมัครสมาชิก ChatGPT"),
    ("onboarding.complete.auth.api", "API key reference", "อ้างอิง API key"),
    ("onboarding.complete.auth.local", "none (local)", "ไม่มี (ในเครื่อง)"),
    # S12 — jarn --help (flag names / command names stay English)
    (
        "cli.description",
        "J.A.R.N. — Just A Reliable Nerd (coding agent TUI)",
        "J.A.R.N. — Just A Reliable Nerd (เอเจนต์เขียนโค้ดในเทอร์มินัล)",
    ),
    (
        "cli.footer",
        "See `jarn <command> --help` for subcommand flags.",
        "ดู `jarn <command> --help` สำหรับแฟล็กของคำสั่งย่อย",
    ),
    ("cli.group.commands", "Commands", "คำสั่ง"),
    ("cli.group.start", "Start", "เริ่มต้น"),
    ("cli.group.oneshot", "One-shot", "ครั้งเดียว"),
    ("cli.group.account", "Account", "บัญชี"),
    ("cli.group.install", "Install", "ติดตั้ง"),
    ("cli.group.workspace", "Workspace", "เวิร์กสเปซ"),
    ("cli.group.gateway", "Gateway", "เกตเวย์"),
    ("cli.group.support", "Support", "ช่วยเหลือ"),
    ("cli.group.options", "options", "ตัวเลือก"),
    ("cli.group.input", "Input", "อินพุต"),
    ("cli.group.output", "Output", "เอาต์พุต"),
    ("cli.group.run", "Run", "รัน"),
    ("cli.group.repair", "Repair", "ซ่อม"),
    ("cli.group.checks", "Checks", "ตรวจ"),
    ("cli.group.scope", "Scope", "ขอบเขต"),
    ("cli.group.confirm", "Confirm", "ยืนยัน"),
    ("cli.group.release", "Release", "รุ่น"),
    ("cli.group.mode", "Mode", "โหมด"),
    ("cli.group.wait", "Wait", "รอ"),
    ("cli.group.method", "Method", "วิธี"),
    ("cli.group.refresh", "Refresh", "รีเฟรช"),
    ("cli.group.setup", "Setup", "ตั้งค่า"),
    ("cli.epilog.start.title", "Start and common commands:", "เริ่มต้นและคำสั่งที่ใช้บ่อย:"),
    (
        "cli.epilog.start.body",
        "  jarn setup                       verified, resumable first-run setup\n"
        "  jarn gateway setup               verify a Telegram bot, discover your user ID,\n"
        "                                   store its token safely, and offer auto-start\n"
        "  jarn                             start interactive coding in the current directory\n"
        '  jarn exec "TASK" --mode ask      run one automation-safe, non-interactive turn\n'
        "  jarn sessions                    list saved sessions; add --help for export/delete",
        "  jarn setup                       ตั้งค่าครั้งแรก ตรวจได้และทำต่อได้\n"
        "  jarn gateway setup               ตรวจบอท Telegram หา user ID ของคุณ\n"
        "                                   เก็บโทเค็นให้ปลอดภัย และเสนอให้เปิดอัตโนมัติ\n"
        "  jarn                             เริ่มเขียนโค้ดแบบโต้ตอบในไดเรกทอรีปัจจุบัน\n"
        '  jarn exec "TASK" --mode ask      รันหนึ่งเทิร์นแบบไม่โต้ตอบ ปลอดภัยสำหรับอัตโนมัติ\n'
        "  jarn sessions                    รายการเซสชันที่บันทึก; เพิ่ม --help สำหรับ export/delete",
    ),
    (
        "cli.epilog.install.title",
        "Installation and configuration (no browser required):",
        "ติดตั้งและการตั้งค่า (ไม่ต้องเปิดเบราว์เซอร์):",
    ),
    (
        "cli.epilog.install.body",
        "  jarn doctor --json               show resolved executable path, install method/record,\n"
        "                                   setup state, dependency versions, and PATH conflicts\n"
        "  jarn config path                 print the active global config path\n"
        "  jarn config path --project       print .jarn/config.yaml for this project\n"
        "  jarn config validate             validate configuration without changing it",
        "  jarn doctor --json               แสดงพาธ executable ที่ใช้ วิธี/บันทึกการติดตั้ง\n"
        "                                   สถานะ setup รุ่น dependency และความขัดแย้งใน PATH\n"
        "  jarn config path                 พิมพ์พาธ config ส่วนกลางที่ใช้งาน\n"
        "  jarn config path --project       พิมพ์ .jarn/config.yaml ของโปรเจกต์นี้\n"
        "  jarn config validate             ตรวจการตั้งค่าโดยไม่แก้ไขไฟล์",
    ),
    ("cli.epilog.auth.title", "Authentication:", "การยืนยันตัวตน:"),
    (
        "cli.epilog.auth.body",
        "  jarn auth login [--device]       sign in with a ChatGPT subscription (device for SSH)\n"
        "  jarn auth status                 verify Codex dependency, auth mode, and account\n"
        "  jarn auth repair                 recheck dependency and refresh ChatGPT auth\n"
        "  jarn auth logout                 remove only Codex-managed credentials\n"
        "  jarn login                       OpenRouter OAuth login (separate from ChatGPT auth)",
        "  jarn auth login [--device]       เข้าสู่ระบบด้วย ChatGPT (device สำหรับ SSH)\n"
        "  jarn auth status                 ตรวจ Codex โหมด auth และบัญชี\n"
        "  jarn auth repair                 ตรวจ dependency อีกครั้งแล้วรีเฟรช ChatGPT auth\n"
        "  jarn auth logout                 ลบเฉพาะข้อมูลรับรองที่ Codex จัดการ\n"
        "  jarn login                       เข้าสู่ระบบ OpenRouter แบบ OAuth (แยกจาก ChatGPT)",
    ),
    ("cli.epilog.models.title", "Models and reasoning:", "โมเดลและการคิด:"),
    (
        "cli.epilog.models.body",
        "  In interactive J.A.R.N., /model lists verified models and then offers only\n"
        "  reasoning efforts supported by the chosen model; /model refresh forces a refresh.\n"
        "  Use /status to inspect the active model/reasoning or --model PROFILE/MODEL with exec.",
        "  ในโหมดโต้ตอบ /model แสดงโมเดลที่ตรวจแล้ว แล้วเสนอเฉพาะ\n"
        "  reasoning ที่โมเดลนั้นรองรับ; /model refresh บังคับรีเฟรช\n"
        "  ใช้ /status เพื่อดูโมเดล/reasoning ที่ใช้ หรือ --model PROFILE/MODEL กับ exec",
    ),
    ("cli.epilog.permissions.title", "Permissions and safety:", "สิทธิ์และความปลอดภัย:"),
    (
        "cli.epilog.permissions.body",
        "  /mode plan|ask|auto-edit|yolo changes the interactive mode. With exec, use --mode.\n"
        "  plan = review only; ask = confirm changes (safe default); auto-edit = workspace edits;\n"
        "  yolo = broad access, but hard catastrophic-action and credential guards remain active.",
        "  /mode plan|ask|auto-edit|yolo เปลี่ยนโหมดโต้ตอบ กับ exec ใช้ --mode\n"
        "  plan = ดูอย่างเดียว; ask = ยืนยันก่อนแก้ (ค่าเริ่มต้นที่ปลอดภัย); auto-edit = แก้ในเวิร์กสเปซ;\n"
        "  yolo = เข้าถึงกว้าง แต่ยังบล็อกการทำลายร้ายแรงและข้อมูลรับรอง",
    ),
    (
        "cli.epilog.diagnosis.title",
        "Diagnosis, repair, and support:",
        "วินิจฉัย ซ่อม และขอความช่วยเหลือ:",
    ),
    (
        "cli.epilog.diagnosis.body",
        "  jarn doctor                      offline, non-mutating diagnosis (add --network to opt in)\n"
        "  jarn doctor --fix --dry-run      preview allowlisted, recoverable repairs\n"
        "  jarn doctor --fix                apply the shown plan with backup/rollback protection\n"
        "  jarn doctor --report FILE        write a redacted support report (owner-only mode 0600)\n"
        "  jarn bug --dry-run               prepare local support material without opening a browser",
        "  jarn doctor                      วินิจฉัยออฟไลน์ ไม่แก้ไฟล์ (เพิ่ม --network เพื่อเลือกเข้า)\n"
        "  jarn doctor --fix --dry-run      ดูแผนซ่อมที่อนุญาตและย้อนกลับได้\n"
        "  jarn doctor --fix                ใช้แผนที่แสดง พร้อมสำรองและ rollback\n"
        "  jarn doctor --report FILE        เขียนรายงานซัพพอร์ตที่ปิดข้อมูล (โหมดเจ้าของ 0600)\n"
        "  jarn bug --dry-run               เตรียมไฟล์ซัพพอร์ตท้องถิ่นโดยไม่เปิดเบราว์เซอร์",
    ),
    (
        "cli.epilog.update.title",
        "Update, rollback, and removal:",
        "อัปเดต ย้อนกลับ และถอนการติดตั้ง:",
    ),
    (
        "cli.epilog.update.body",
        "  jarn update --check              check only; jarn update --dry-run previews activation\n"
        "  jarn rollback                    activate the retained previous working version\n"
        "  jarn uninstall                   choose components; config/data/credentials are kept by default\n"
        "  jarn uninstall --help            show explicit data-removal category flags",
        "  jarn update --check              ตรวจอย่างเดียว; jarn update --dry-run ดูแผนการเปิดใช้\n"
        "  jarn rollback                    สลับไปรุ่นก่อนหน้าที่ทำงานได้ซึ่งเก็บไว้\n"
        "  jarn uninstall                   เลือกส่วนที่จะถอด; config/ข้อมูล/credentials ถูกเก็บเป็นค่าเริ่มต้น\n"
        "  jarn uninstall --help            แสดงแฟล็กหมวดการลบข้อมูลแบบชัดเจน",
    ),
    ("cli.epilog.exits.title", "Stable exit codes:", "รหัสออกที่คงที่:"),
    (
        "cli.epilog.exits.body",
        "  0 success; 1 internal/diagnostic issue; 2 usage or configuration;\n"
        "  3 auth; 4 model unavailable; 5 permission denied; 6 network/provider;\n"
        "  7 update/rollback failed; 8 budget exceeded; 9 verification failed;\n"
        "  10 updated executable requires a fresh shell; 124 timeout; 130 cancelled.",
        "  0 สำเร็จ; 1 ปัญหาภายใน/วินิจฉัย; 2 การใช้หรือการตั้งค่า;\n"
        "  3 auth; 4 โมเดลใช้ไม่ได้; 5 ไม่ได้รับอนุญาต; 6 เครือข่าย/ผู้ให้บริการ;\n"
        "  7 update/rollback ล้มเหลว; 8 เกินงบ; 9 ตรวจยืนยันไม่ผ่าน;\n"
        "  10 ไฟล์ใหม่ต้องเปิดเชลล์ใหม่; 124 หมดเวลา; 130 ถูกยกเลิก",
    ),
    ("cli.cmd.setup", "Run the onboarding wizard", "เปิดตัวช่วยตั้งค่าครั้งแรก"),
    ("cli.cmd.init", "Create a JARN.md project context file", "สร้างไฟล์บริบทโปรเจกต์ JARN.md"),
    ("cli.cmd.exec", "Run one non-interactive agent turn", "รันหนึ่งเทิร์นแบบไม่โต้ตอบ"),
    (
        "cli.cmd.sessions",
        "List, export, or delete saved sessions",
        "ดู ส่งออก หรือลบเซสชันที่บันทึก",
    ),
    (
        "cli.cmd.auth",
        "Sign in, verify, repair, or sign out of ChatGPT",
        "เข้าสู่ระบบ ตรวจ ซ่อม หรือออกจาก ChatGPT",
    ),
    (
        "cli.cmd.login",
        "Log in to OpenRouter via OAuth PKCE — opens your browser, "
        "catches the callback, and stores the API key in the OS keychain",
        "เข้าสู่ระบบ OpenRouter ผ่าน OAuth PKCE — เปิดเบราว์เซอร์ รับ callback แล้วเก็บ API key ใน keychain",
    ),
    ("cli.cmd.codex", "Compatibility alias for `jarn auth`", "ชื่อสำรองของ `jarn auth`"),
    (
        "cli.cmd.doctor",
        "Diagnose configuration and providers",
        "ตรวจการตั้งค่าและผู้ให้บริการ",
    ),
    (
        "cli.cmd.config",
        "Inspect or safely manage configuration",
        "ดูหรือจัดการการตั้งค่าอย่างปลอดภัย",
    ),
    (
        "cli.cmd.update",
        "Check for or transactionally install an update",
        "ตรวจหรือติดตั้งอัปเดตแบบทำรายการ",
    ),
    (
        "cli.cmd.rollback",
        "Activate the retained previous version",
        "สลับไปรุ่นก่อนหน้าที่เก็บไว้",
    ),
    (
        "cli.cmd.uninstall",
        "Remove selected J.A.R.N. components while retaining user data by default",
        "ถอดส่วนที่เลือก โดยค่าเริ่มต้นยังเก็บข้อมูลผู้ใช้",
    ),
    (
        "cli.cmd.trust",
        "List, trust, or untrust project roots (capability gate)",
        "ดู เชื่อถือ หรือเลิกเชื่อถือรากโปรเจกต์ (เกตความสามารถ)",
    ),
    (
        "cli.cmd.trust-hooks",
        "Record a one-time accept to run global lifecycle hooks "
        "(enables `hook_global_require_trust: true`)",
        "ยอมรับครั้งเดียวให้รัน global lifecycle hooks (เปิด `hook_global_require_trust: true`)",
    ),
    (
        "cli.cmd.keys",
        "Key inspector — see what your terminal sends for each key",
        "ตัวตรวจคีย์ — ดูที่เทอร์มินัลส่งมาแต่ละปุ่ม",
    ),
    (
        "cli.cmd.gateway",
        "Set up, inspect, or run the Telegram gateway",
        "ตั้งค่า ตรวจ หรือรันเกตเวย์ Telegram",
    ),
    (
        "cli.cmd.bug",
        "Write a privacy-scanned local report and, with consent, open a "
        "content-free GitHub issue template",
        "เขียนรายงานท้องถิ่นที่สแกนความเป็นส่วนตัว และถ้าอนุญาต เปิดเทมเพลต issue บน GitHub ที่ไม่มีเนื้อหา",
    ),
    (
        "cli.cmd.telemetry",
        "Inspect or change opt-in local-only telemetry",
        "ดูหรือเปลี่ยนเทเลเมทรีท้องถิ่นแบบ opt-in",
    ),
    (
        "cli.cmd.completions",
        "Emit a shell completion script for bash, zsh, or fish",
        "สร้างสคริปต์เติมคำสำหรับ bash, zsh หรือ fish",
    ),
    ("cli.flag.help", "show this help message and exit", "แสดงข้อความช่วยเหลือนี้แล้วออก"),
    (
        "cli.flag.version",
        "show program's version number and exit",
        "แสดงเลขรุ่นของโปรแกรมแล้วออก",
    ),
    (
        "cli.flag.resume",
        "Pick a previous session to resume on launch",
        "เลือกเซสชันก่อนหน้าเพื่อทำต่อตอนเปิด",
    ),
    (
        "cli.flag.add_dir",
        "Add a directory to the session's write scope (repeatable). Each dir "
        "becomes an active root the agent may edit, alongside the project "
        "root. Checkpoint/undo and project context stay primary-root only.",
        "เพิ่มไดเรกทอรีเข้าขอบเขตเขียนของเซสชัน (ซ้ำได้) แต่ละไดเรกทอรีเป็นรากที่"
        "เอเจนต์แก้ได้ คู่กับรากโปรเจกต์ Checkpoint/undo และบริบทโปรเจกต์ยังอยู่ที่รากหลักเท่านั้น",
    ),
    (
        "cli.flag.print",
        "Run a single non-interactive turn and print the result. "
        "Pass '-' to read the prompt from stdin.",
        "รันหนึ่งเทิร์นแบบไม่โต้ตอบแล้วพิมพ์ผลลัพธ์ ส่ง '-' เพื่ออ่านพรอมต์จาก stdin",
    ),
    (
        "cli.flag.output_format",
        "With -p: output format (text|json|stream-json; default text). "
        "'json' emits one buffered final object; 'stream-json' emits NDJSON — "
        "one JSON object per event as the turn runs, then a terminal "
        '{"type":"result",...} line (with thread_id) — mirroring '
        "`claude -p --output-format stream-json`.",
        "ใช้กับ -p: รูปแบบเอาต์พุต (text|json|stream-json; ค่าเริ่มต้น text) "
        "'json' ส่งอ็อบเจกต์สุดท้ายก้อนเดียว; 'stream-json' ส่ง NDJSON — "
        "หนึ่ง JSON ต่อเหตุการณ์ แล้วจบด้วยบรรทัด "
        '{"type":"result",...} (มี thread_id) — สอดคล้อง '
        "`claude -p --output-format stream-json`",
    ),
    (
        "cli.flag.json",
        "With -p: legacy alias for --output-format json. On success: "
        "{result, tokens, cost, turns, tool_calls, verification}. On failure: "
        "{error: {kind, message}}.",
        "ใช้กับ -p: ชื่อสำรองของ --output-format json เมื่อสำเร็จ: "
        "{result, tokens, cost, turns, tool_calls, verification} เมื่อล้มเหลว: "
        "{error: {kind, message}}",
    ),
    (
        "cli.flag.model",
        "Override the active model for this headless run.",
        "ทับโมเดลที่ใช้สำหรับรันแบบ headless นี้",
    ),
    (
        "cli.flag.mode",
        "With -p: override the permission mode (plan|ask|auto-edit|yolo). "
        "Note: --preset overrides this for trust-relevant knobs if both are given.",
        "ใช้กับ -p: ทับโหมดสิทธิ์ (plan|ask|auto-edit|yolo) "
        "หมายเหตุ: --preset ทับค่าที่เกี่ยวกับความเชื่อถือถ้าส่งทั้งคู่",
    ),
    (
        "cli.flag.preset",
        "Apply a preset — a launch-time shortcut that sets mode + sandbox "
        "(trusted-repo|review-only|sandbox-required|ci|offline).",
        "ใช้ preset — ทางลัดตอนเปิดที่ตั้ง mode + sandbox "
        "(trusted-repo|review-only|sandbox-required|ci|offline)",
    ),
    (
        "cli.flag.max_turns",
        "With -p: must be 1 (the default); values >1 are rejected. A headless "
        "invocation always runs exactly one complete model/tool graph turn.",
        "ใช้กับ -p: ต้องเป็น 1 (ค่าเริ่มต้น); ค่า >1 ถูกปฏิเสธ การเรียก headless "
        "รันกราฟโมเดล/เครื่องมือครบหนึ่งเทิร์นเท่านั้น",
    ),
    (
        "cli.flag.cwd",
        "Working directory for this headless run.",
        "ไดเรกทอรีทำงานสำหรับรันแบบ headless นี้",
    ),
    (
        "cli.flag.ignore_project_config",
        "Ignore <cwd>/.jarn/config.yaml while still operating on the project "
        "files (safe for automation on untrusted checkouts).",
        "ไม่ใช้ <cwd>/.jarn/config.yaml แต่ยังทำงานกับไฟล์โปรเจกต์ "
        "(ปลอดภัยสำหรับอัตโนมัติบน checkout ที่ไม่น่าเชื่อถือ)",
    ),
    (
        "cli.flag.resume_session",
        "With -p: resume a prior headless thread. Pass 'last' for the most "
        "recent session or a thread id from /sessions. An empty prompt "
        "continues without a new user message.",
        "ใช้กับ -p: ทำต่อเธรด headless ก่อนหน้า ส่ง 'last' สำหรับเซสชันล่าสุด "
        "หรือ thread id จาก /sessions พรอมต์ว่างจะทำต่อโดยไม่มีข้อความผู้ใช้ใหม่",
    ),
    (
        "cli.flag.output_schema",
        "With -p: path to a JSON Schema file. Constrains the agent's final "
        "answer to the schema; the parsed object is returned as 'result' in "
        "the --json envelope (exit 9 with kind 'schema' if the agent fails "
        "to produce a conforming response).",
        "ใช้กับ -p: พาธไฟล์ JSON Schema จำกัดคำตอบสุดท้ายให้ตรงสคีมา "
        "อ็อบเจกต์ที่แปลงแล้วถูกส่งเป็น 'result' ในซอง --json "
        "(exit 9 ชนิด 'schema' ถ้าเอเจนต์สร้างคำตอบที่ตรงสคีมาไม่ได้)",
    ),
    # S14 — Telegram local slash pages (/help /status /mode) + mutating hint
    ("telegram.help.group", "Gateway", "เกตเวย์"),
    ("telegram.help.stop", "Cancel the in-flight turn", "ยกเลิกเทิร์นที่กำลังรัน"),
    ("telegram.help.new", "Start a fresh thread", "เริ่มเธรดใหม่"),
    (
        "telegram.help.reset",
        "Alias of /new (fresh gateway thread, not /clear)",
        "นามแฝงของ /new (เธรดเกตเวย์ใหม่ ไม่ใช่ /clear)",
    ),
    ("telegram.help.repo", "Switch the active repo", "สลับรีโปที่ใช้อยู่"),
    (
        "telegram.help.help",
        "This catalog (same commands as the REPL)",
        "แค็ตตาล็อกนี้ (คำสั่งเดียวกับ REPL)",
    ),
    (
        "telegram.help.rollback",
        "Alias: use /checkpoints and /undo — not a mutate command",
        "นามแฝง: ใช้ /checkpoints และ /undo — ไม่ใช่คำสั่งแก้ไข",
    ),
    (
        "telegram.mutating",
        "This command is not available on Telegram. Use the terminal / jarn CLI.",
        "คำสั่งนี้ใช้บน Telegram ไม่ได้ ใช้เทอร์มินัล / jarn CLI",
    ),
    (
        "telegram.mutating.named",
        "/{name} is not available on Telegram. Use the terminal / jarn CLI.",
        "/{name} ใช้บน Telegram ไม่ได้ ใช้เทอร์มินัล / jarn CLI",
    ),
    ("status.title", "Status", "สถานะ"),
    ("status.resume_title", "Resumed", "ทำต่อแล้ว"),
    ("status.directory", "Directory", "โฟลเดอร์"),
    ("status.model", "Model", "โมเดล"),
    ("status.provider", "Provider", "ผู้ให้บริการ"),
    ("status.reasoning", "Reasoning", "Reasoning"),
    ("status.permissions", "Permissions", "สิทธิ์"),
    ("status.workspace", "Workspace", "เวิร์กสเปซ"),
    ("status.context", "Context", "บริบท"),
    ("status.session", "Session", "เซสชัน"),
    ("status.compact", "Compact", "Compact"),
    ("status.recap", "Recap", "สรุป"),
    ("status.tools", "Tools", "เครื่องมือ"),
    ("status.calls", "Calls", "การเรียก"),
    ("status.files", "Files", "ไฟล์"),
    ("status.last_you", "Last you", "คุณล่าสุด"),
    ("status.last_jarn", "Last J.A.R.N.", "J.A.R.N. ล่าสุด"),
    ("status.not_configured", "not configured", "ยังไม่ตั้งค่า"),
    ("status.provider_default", "provider default", "ค่าเริ่มต้นของผู้ให้บริการ"),
    ("status.not_measured", "not measured", "ยังไม่วัด"),
    ("status.trusted", "trusted", "trusted"),
    (
        "status.untrusted_floor",
        "untrusted (read-only floor)",
        "untrusted (อ่านอย่างเดียว)",
    ),
    ("status.turn_one", "1 turn", "1 เทิร์น"),
    ("status.turn_many", "{n} turns", "{n} เทิร์น"),
    (
        "status.compact_value",
        "{n}  ·  /compact applies (in-graph auto-compact is not counted)",
        "{n}  ·  /compact ใช้ได้ (auto-compact ในกราฟไม่นับ)",
    ),
    (
        "status.auth.chatgpt",
        "ChatGPT subscription (Codex-managed; /login to verify)",
        "สมัคร ChatGPT (Codex จัดการ; /login เพื่อยืนยัน)",
    ),
    (
        "status.auth.local",
        "local endpoint (no cloud key)",
        "endpoint ในเครื่อง (ไม่มีคีย์คลาวด์)",
    ),
    ("status.auth.api_key", "API-key reference", "อ้างอิงคีย์ API"),
    ("mode.title", "Mode", "โหมด"),
    ("mode.label.plan", "Review only", "ตรวจอย่างเดียว"),
    ("mode.label.ask", "Ask before changes", "ถามก่อนเปลี่ยน"),
    ("mode.label.auto-edit", "Edit workspace", "แก้เวิร์กสเปซ"),
    ("mode.label.yolo", "Full access", "เข้าถึงเต็มที่"),
    (
        "mode.current",
        "Current permissions: {summary}",
        "สิทธิ์ปัจจุบัน: {summary}",
    ),
    (
        "mode.unknown",
        "Unknown mode. Choose one of: {valid}",
        "ไม่รู้จักโหมด เลือกจาก: {valid}",
    ),
    (
        "mode.set",
        "Permissions set to {summary} (rebuilding).",
        "ตั้งสิทธิ์เป็น {summary} แล้ว (กำลังสร้างใหม่)",
    ),
    (
        "mode.set_id",
        "Permission mode set to {mode} (rebuilding).",
        "ตั้งโหมดสิทธิ์เป็น {mode} แล้ว (กำลังสร้างใหม่)",
    ),
    (
        "mode.untrusted",
        "Project untrusted — mode clamped to {mode}. "
        "Run `jarn trust` to unlock other modes. (rebuilding)",
        "โปรเจกต์นี้ untrusted — โหมดถูกจำกัดเป็น {mode} รัน `jarn trust` เพื่อปลดโหมดอื่น (กำลังสร้างใหม่)",
    ),
    (
        "mode.yolo_sync_refused",
        "Escalating to yolo requires confirmation — "
        "use await controller.set_permission_mode('yolo', confirm=…).",
        "การยกระดับเป็น yolo requires confirmation — "
        "ใช้ await controller.set_permission_mode('yolo', confirm=…)",
    ),
    (
        "mode.yolo_async_refused",
        "Escalating to yolo requires confirmation — "
        "pass confirm=… to set_permission_mode "
        "(sync handle_command('mode','yolo') refuses this path).",
        "การยกระดับเป็น yolo requires confirmation — "
        "ส่ง confirm=… ให้ set_permission_mode "
        "(sync handle_command('mode','yolo') ปฏิเสธเส้นทางนี้)",
    ),
    (
        "mode.yolo_cancelled",
        "yolo cancelled — mode unchanged.",
        "ยกเลิก yolo — โหมดไม่เปลี่ยน",
    ),
)

EN: Mapping[str, str] = MappingProxyType({key: en for key, en, _th in _STRINGS})
TH: Mapping[str, str] = MappingProxyType({key: th for key, _en, th in _STRINGS})
CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType({"en": EN, "th": TH})

if EN.keys() != TH.keys():
    missing_en = set(TH) - set(EN)
    missing_th = set(EN) - set(TH)
    raise RuntimeError(
        f"i18n catalogs drifted: missing in en={sorted(missing_en)} "
        f"missing in th={sorted(missing_th)}"
    )


def _locale_setting(config: object | None) -> str:
    if config is None:
        return "auto"
    if isinstance(config, str):
        return config
    ui = getattr(config, "ui", None)
    raw = getattr(ui, "locale", None) if ui is not None else getattr(config, "locale", None)
    if raw is None:
        return "auto"
    return str(raw)


def _env_locale(environ: Mapping[str, str]) -> str:
    """POSIX message-locale: LC_ALL, then LC_MESSAGES, then LANG."""
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = environ.get(key, "")
        if value.strip():
            return value
    return ""


def _is_thai_locale(value: str) -> bool:
    raw = value.strip()
    if not raw:
        return False
    lang = raw.split(".", 1)[0].split("@", 1)[0].split("_", 1)[0].split("-", 1)[0]
    return lang.lower() == "th"


def resolve_locale(
    config: object | None = None,
    environ: Mapping[str, str] | None = None,
) -> Locale:
    """Resolve ``ui.locale`` to ``en`` or ``th``.

    ``auto`` follows ``LC_ALL``, then ``LC_MESSAGES``, then ``LANG``. A value
    whose language tag is ``th`` (e.g. ``th_TH.UTF-8``) yields ``th``;
    everything else yields ``en``.
    """
    setting = _locale_setting(config)
    if setting not in LOCALE_SETTINGS:
        raise ValueError(f"ui.locale must be one of {list(LOCALE_SETTINGS)} (got {setting!r}).")
    if setting == "en":
        return "en"
    if setting == "th":
        return "th"
    env = os.environ if environ is None else environ
    if _is_thai_locale(_env_locale(env)):
        return "th"
    return "en"


def t(key: str, locale: str | None = None, **kwargs: object) -> str:
    """Look up a chrome string.

    *locale* is ``en`` or ``th``. ``None`` or ``auto`` follows
    :func:`resolve_locale`. Missing keys raise :class:`KeyError`.
    """
    loc: str
    if locale is None or locale == "auto":
        loc = resolve_locale("auto", os.environ)
    elif locale in CATALOGS:
        loc = locale
    else:
        raise ValueError(f"unknown locale {locale!r}; expected 'en' or 'th'")
    try:
        template = CATALOGS[loc][key]
    except KeyError:
        raise KeyError(f"missing i18n key {key!r} for locale {loc!r}") from None
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except KeyError as exc:
        raise KeyError(f"missing format field {exc} for i18n key {key!r}") from None
