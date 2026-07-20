<div align="center">

```
     ██╗  █████╗  ██████╗  ███╗   ██╗
     ██║ ██╔══██╗ ██╔══██╗ ████╗  ██║
     ██║ ███████║ ██████╔╝ ██╔██╗ ██║
██   ██║ ██╔══██║ ██╔══██╗ ██║╚██╗██║
╚█████╔╝ ██║  ██║ ██║  ██║ ██║ ╚████║
 ╚════╝  ╚═╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝  ╚═══╝
```

**J.A.R.N. — Just A Reliable Nerd**

TUI-first coding agent harness ที่สร้างบน [DeepAgents](https://github.com/langchain-ai/deepagents)

[English](README.md) · **ภาษาไทย**

![jarn demo](docs/assets/demo.gif)

</div>

---

J.A.R.N. คือ terminal coding agent ที่ออกแบบในแนวทางเดียวกับ Claude Code และ Codex CLI แต่สร้างขึ้นเป็น harness ของตัวเองบน DeepAgents library จุดเด่นที่แตกต่างคือ **ความน่าเชื่อถือ (reliability)**: agent จะวางแผนก่อนลงมือ, ตรวจสอบผลลัพธ์ของตัวเอง, ขอ permission ก่อนทำทุกอย่างที่มีความเสี่ยง และไม่อ้างว่าทำสำเร็จแบบเดา

รันทั้งหมดใน terminal ของคุณ (Web UI อยู่ใน roadmap หลัง launch) ความสามารถเด่นได้แก่: **AGENTS.md / CLAUDE.md interop** (ทำงานร่วมกับ agent อื่นได้ทันที), **headless one-shot mode** (`jarn -p "..."`), **JSONL session transcript**, **`!` shell escape** (output ถูกส่งเข้า context ของ agent turn ถัดไปโดยอัตโนมัติ), **OS-level execution sandbox** (macOS `sandbox-exec` / Linux `bwrap`) และ **Docker container backend** (`execution.backend: docker`), **presets** (`/preset`, `jarn --preset`) ที่ตั้ง mode + sandbox พร้อมกันในคำสั่งเดียวพร้อม untrusted floor, **auto-checkpoint + `/undo` / `/redo`**, **repo map** (`/map`), **wiki knowledge base** (`/wiki`), **`/config` settings panel** (UI แบบ tab โต้ตอบได้ เซฟลง `~/.jarn/config.yaml`), และ **MCP health** ราย server (`/mcp status`)

> **สถานะ:** v0.9.1 (Alpha) — อยู่บน PyPI (`pip install jarn`) และ npm (`npm install -g jarn-cli` — binary สำเร็จรูป ไม่ต้องมี Python) v0.9 เป็น **hardening release**: audit ตัวเองรอบใหญ่ + adversarial review ข้ามโมเดล (47 fix ที่พิสูจน์ด้วย repro ครอบคลุม permissions, cost integrity, concurrency, compaction, observability; ชื่อ MCP tool เปลี่ยนเป็น `mcp__<server>__<tool>` — ดู breaking note ใน CHANGELOG) สาม release ก่อนหน้า (v0.6.0–v0.8.0) รวม 4 รอบปรับปรุงใหญ่: **engine reliability**; **UX parity กับ Claude Code** (live in-place streaming, `/theme`, `@git:`/`@url:` mentions, word-level diff, ghost autosuggest, `/rewind` แบบกู้ไฟล์ได้); **differentiators** (`--add-dir` multi-root, inline images, headless `--output-schema`, subagent streaming แบบมีป้ายชื่อ, web search แบบ pluggable, LSP-lite diagnostics loop, verified-completion badge); และ **launch systems** (GitHub Action ที่ reuse ได้ + nightly eval harness) สถาปัตยกรรม, config, permission engine, และ terminal REPL ทำเสร็จและมีเทสต์ครบ; การเรียก model จริงต้องใช้ API key ของคุณเอง ดู [CHANGELOG.md](CHANGELOG.md) และ [SECURITY.md](SECURITY.md)

> **ความปลอดภัย:** J.A.R.N. รัน tool บน **host** ของคุณโดยตรงเป็นค่าเริ่มต้น (filesystem + shell จริง) `.jarn/config.yaml` ของโปรเจกต์สามารถประกาศ hook, MCP server, และ provider override ได้ — ควร trust เฉพาะ repository ที่คุณยอมรันโค้ดจากมันเท่านั้น โปรเจกต์ที่ยังไม่ trust จะถูกกั้นไว้จนกว่าคุณจะอนุมัติ (`jarn trust`) โปรดอ่าน [SECURITY.md](SECURITY.md) ก่อนใช้งาน

## ทำไมต้องเลือก J.A.R.N.?

- **Reliable by design** — flow แบบ plan → act → verify ฝังอยู่ใน system prompt ค่าเริ่มต้น `verify.gate: suggest` จะแสดงคำสั่งตรวจสอบที่ตรวจพบ ส่วน `verify.gate: auto` จะรันคำสั่งนั้นก่อนจบงาน หากไม่ผ่าน ระบบจะส่งผลกลับให้ agent แก้แบบจำกัดรอบ และจบ headless run ด้วย error หากยังไม่ผ่าน completion badge `` ⎿ verified: pytest ✓ 214 passed · 3.2s `` จึงยืนยันผลที่รันจริง และมี diagnostics feedback loop (LSP-lite) สำหรับไฟล์ที่แก้ (`verify.diagnostics: auto`)
- **ปลอดภัยเป็นค่าเริ่มต้น** — ระบบ permission หลายชั้น (coarse mode + fine-grained rules) คั่นกลางทุก file write และ shell command โดยมี *danger-guard* ที่ยืนยันการกระทำร้ายแรงเสมอ — แม้แต่ใน YOLO mode
- **เลือก model เองได้ (Bring your own model)** — รองรับ 13 provider (OpenRouter, Anthropic, OpenAI, Google, Mistral, Groq, DeepSeek, Together, Fireworks, xAI, Ollama, LM Studio, และ generic OpenAI-compatible endpoint) พร้อม per-task routing ให้ subagent ใช้ model ที่ถูกกว่าได้
- **สตรีม subagent แบบมีป้ายกำกับ** — output จาก subagent ที่ถูก delegate ผ่าน `task` จะถูกติดป้ายด้วย prefix สีจาง `┊ <name> ` และยุบเป็นบรรทัดสถานะเดียว `└ <name>: working… (N tool calls)` (ข้อความเต็มดูได้ใน pager ด้วย Ctrl+O) ทำให้ subagent ที่รันขนานกันไม่ปนกันแบบไม่มีชื่ออีกต่อไป
- **รู้ต้นทุนและ context ตลอดเวลา** — ติดตาม token/cost แบบ live (พร้อม breakdown ราย tool) และ budget ต่อ session ที่แจ้งเตือนหรือหยุดอัตโนมัติได้; มี context-% gauge และ throughput การ generate แบบ live (tok/s) ที่ทำงานกับ local model (LM Studio / Ollama) ด้วย ไม่ใช่แค่ cloud model ที่มีราคา
- **รู้วันเวลา (Date-aware)** — วันที่/เวลาท้องถิ่นปัจจุบันถูกใส่เข้า system prompt ทำให้คำสั่งที่อ้างอิง "วันนี้" ไม่ยึดติดกับ training cutoff ของ model
- **ค้นหาเว็บแบบ Pluggable** — `web_search` รองรับ Tavily, Brave Search, และ Exa นอกจาก DuckDuckGo แบบ keyless ที่ใช้เป็น fallback  ตั้งค่า `search.provider: auto` (ค่าเริ่มต้น) แล้ว export `TAVILY_API_KEY` / `BRAVE_API_KEY` / `EXA_API_KEY` — ตัวแรกที่มีค่าจะถูกใช้งาน
- **ขยายได้ง่าย** — skill, slash command, custom subagent, lifecycle hook, และ MCP server ทั้งหมดกำหนดผ่านไฟล์ธรรมดาใน `~/.jarn` และ `.jarn/`

## ติดตั้ง

รองรับ macOS (Apple Silicon) และ Linux (x64 / arm64); บน Windows ใช้ผ่าน WSL

**ผ่าน npm** — เป็น binary สำเร็จรูป **ไม่ต้องมี Python**:

```bash
npm install -g jarn-cli     # ได้คำสั่ง `jarn` (ใช้ `jarn-cli` ก็ได้)
```

Intel mac ให้ติดตั้งผ่าน pip/uv แทน (ไม่มี binary npm สำหรับ Intel mac)

**ผ่าน pip / uv** — ต้องการ **Python 3.12+** และ [uv](https://docs.astral.sh/uv/):

```bash
pip install jarn            # PyPI (alpha)
# หรือ: uv tool install jarn
```

**ติดตั้งจาก source:**

```bash
git clone https://github.com/chayapats/jarn && cd jarn
uv sync --extra dev
uv run jarn
```

`uv.lock` ถูก track ไว้ใน repo เพื่อให้ทุกคนในทีมได้ dependency เวอร์ชันเดียวกัน

### แชร์กับทีม

```bash
git clone <repo-url> && cd jarn
uv sync --extra dev
uv run jarn setup          # ทำครั้งเดียวต่อเครื่อง — เก็บ API key ใน ~/.jarn
cd your-project
jarn doctor                # ตรวจสอบ config, provider, และ extension ที่โหลดอยู่
jarn                       # จะถามให้ trust ถ้าโปรเจกต์ประกาศ hook/MCP
jarn trust .               # pre-approve repo ที่คุณควบคุมเอง (ไม่บังคับ)
```

ถ้า repo ที่ clone มามี `.jarn/config.yaml` ที่ประกาศ hook, MCP server หรือ provider override J.A.R.N. จะถามก่อนทำตาม คุณสามารถปฏิเสธเพื่อรันต่ออย่างปลอดภัยโดยตัดค่าเหล่านั้นทิ้ง หรือรัน `jarn trust <path>` หลังตรวจสอบ repo แล้ว ใช้ `jarn doctor` เพื่อดูว่า skill, command, subagent, hook, และ MCP server ใดจะถูกโหลด (รวมถึงไฟล์ที่ถูกบังหรือถูกข้าม)

## ถอนการติดตั้ง (Uninstall)

หากต้องการลบ state ทั้งหมดของ J.A.R.N. (config, secrets, trust store, sessions) และ keychain entries ในระบบ:

```bash
jarn uninstall          # แสดงรายการสิ่งที่จะถูกลบ แล้วถามยืนยัน
jarn uninstall --yes    # ข้ามการถามยืนยัน
```

`jarn uninstall` ลบเฉพาะ `~/.jarn` (global state) เท่านั้น — ไม่แตะ `.jarn/` ระดับโปรเจกต์เลย หลังลบเสร็จจะแสดงคำสั่ง package-manager สำหรับถอนตัว jarn เอง (`npm uninstall -g jarn-cli` หรือ `pip uninstall jarn`)

## เริ่มใช้งาน (Quick Start)

**ด้วย OpenRouter (แนะนำ — คลิกเดียวในเบราว์เซอร์ ไม่ต้อง paste key เอง):**

```bash
jarn login        # เปิดเบราว์เซอร์ → อนุญาต → เก็บ key ใน OS keychain อัตโนมัติ
cd your-project
jarn              # เปิด TUI (จะรัน setup ถ้ายังไม่ได้ตั้งค่า)
```

**หรือตั้งค่าด้วยตนเอง:**

```bash
jarn setup        # wizard แรกเริ่ม: เลือก provider, เก็บ API key, ตั้งค่า default
cd your-project
jarn init         # สร้างไฟล์ JARN.md สำหรับ project context (ไม่บังคับ แต่แนะนำ)
jarn              # เปิด TUI
jarn doctor       # ตรวจสอบ config / provider / key / extension ได้ตลอดเวลา
jarn bug          # รวมรายงาน bug แบบ redacted + เปิดฟอร์ม GitHub issue แบบกรอกข้อมูลล่วงหน้า
```

**Shell completions (tab-complete subcommand และ flag):**

```bash
# zsh — รันครั้งเดียว แล้วรีสตาร์ท shell
jarn completions zsh > ~/.zfunc/_jarn
# เพิ่มใน ~/.zshrc: fpath=(~/.zfunc $fpath) && autoload -Uz compinit && compinit

# bash — รันครั้งเดียว แล้ว source หรือรีสตาร์ท shell
jarn completions bash > ~/.bash_completions/jarn.bash
# เพิ่มใน ~/.bashrc: source ~/.bash_completions/jarn.bash

# fish — รันครั้งเดียว
jarn completions fish > ~/.config/fish/completions/jarn.fish
```

ถ้ายังไม่มี config เลย J.A.R.N. จะรัน setup wizard ให้อัตโนมัติในการเปิดครั้งแรก
ใน wizard ตัวเลือก OpenRouter จะมีปุ่ม "Log in with browser" ให้ด้วย

## Non-interactive / scripting (โหมด headless)

```bash
jarn -p "summarise the open TODOs"          # one-shot: พิมพ์คำตอบแล้วออก
echo "what changed?" | jarn -p -            # อ่าน prompt จาก stdin
jarn -p "do X" --json                        # output เป็น JSON: {result, tokens, cost, turns}
jarn -p "do X" --model anthropic/claude-opus-4-8  # override model สำหรับรันนี้
jarn -p "do X" --permission-mode auto-edit  # อนุญาต file write โดยไม่ถาม
jarn -p "do X" --cwd /path/to/project       # กำหนด working directory
```

**Fail-closed safety:** mode เริ่มต้น (`ask` / `plan`) จะปฏิเสธ tool ใด ๆ ที่ปกติต้องขออนุมัติ และออกด้วย non-zero exit code ถ้าต้องการให้ทำงานโดยไม่มีคนดู ให้ใช้ `--permission-mode auto-edit` หรือ `yolo` — danger-guard ยังบล็อกคำสั่งร้ายแรงในทุก mode อยู่ดี

## บน CI

J.A.R.N. มี [GitHub Actions composite action](action/action.yml) ให้ใช้ในทุก workflow — PR review, issue-fix bot, nightly audit

```yaml
- uses: chayapats/jarn/action@main
  with:
    prompt: "Review this diff: …"
    preset: "review-only"     # อ่านอย่างเดียว; ใช้ 'ci' สำหรับรันที่เขียนไฟล์ได้
    max_turns: "5"
    api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

**Outputs:** `result`, `cost_usd`, `turns`

**หมายเหตุ Docker:** preset `ci` (ค่าเริ่มต้น) ต้องใช้ Docker (ubuntu runner มีให้) สำหรับ runner ที่ไม่มี Docker (macOS/Windows) ให้ใช้ `preset: trusted-repo` คู่กับ `permission_mode: auto-edit` (mode ที่ระบุชัดจะ override mode ของ preset) — ดู [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md)

ตัวอย่าง workflow: [PR review bot](examples/github/pr-review.yml) ·
[Issue-fix bot](examples/github/issue-fix.yml)
เอกสารเต็ม: [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md)

## หน้าตา interface: native inline

```bash
jarn            # เริ่ม session ใหม่
jarn --resume   # เลือก session เก่าที่ต้องการต่อตอน launch
jarn --add-dir ../shared-lib --add-dir ../sibling-repo  # เพิ่ม writable root (ใส่ซ้ำได้)
```

**Multi-root workspaces (`--add-dir`):** ปกติ write scope ของ agent คือ project root
เท่านั้น ใช้ `--add-dir <dir>` (ใส่ซ้ำได้) เพื่อให้สิทธิ์เขียนไปยัง directory พี่น้องด้วย
(เหมาะกับงาน monorepo/sibling-repo) เพิ่มกลาง session ได้ด้วย `/add-dir <path>`
(ต้องอนุมัติใน `ask` mode; ถูกปฏิเสธถ้า project ยังไม่ trust) root ที่เพิ่มขยายแค่
**write scope** เท่านั้น — project context (JARN.md) และ checkpoint/undo (`/undo`,
`/rewind`) ยังใช้ **primary root เท่านั้น**

J.A.R.N. แสดงผล conversation ลงบน terminal buffer ปกติโดยตรง — ไม่มี alternate screen conversation ทั้งหมดอยู่ใน **native scrollback** ของ terminal: เลื่อนทีเดียวเลื่อนทุกอย่าง และ selection/copy ของ terminal ทำงานได้ตลอดทั้งประวัติ เหมือน Claude Code เป๊ะ reply ของ assistant จะ stream แบบ live และ render เป็น Markdown; tool call, การขออนุมัติ, และ diff preview ต่อ turn แสดงแบบ inline

## วิธีใช้งาน

```
┌ toolbar: model · mode · queue · ctx · cost ─────────────────────────────┐
│                                                                            │
│   conversation stream (assistant output, tool calls, approvals)           │
│                                                                            │
├────────────────────────────────────────────────────────────────────────┤
│ › your message…                                                          │
└────────────────────────────────────────────────────────────────────────┘
```

- **พิมพ์** message แล้วกด **Enter** เพื่อส่ง (**Shift+Enter** / **Ctrl+J** สำหรับขึ้นบรรทัดใหม่)
- ขึ้นต้นบรรทัดด้วย **`/`** เพื่อใช้ command (ดูด้านล่าง) ใช้ **`@`** อ้างอิง file path หรือ
  rich mention:
  - **`@<path>`** — file หรือ directory (ค่าเริ่มต้น; bare `@`)
  - **`@folder:<frag>`** — directory เท่านั้น
  - **`@symbol:<name>`** — symbol จาก repo map (function / class)
  - **`@git:status|diff|staged|log`** — เมื่อส่ง จะถูกแทนที่ด้วย fenced block ของ git output จริง
    (read-only: `--porcelain=v1 -b`, `diff`, `diff --staged`, `log --oneline -15`) ใช้ argv คงที่,
    ไม่ผ่าน shell, timeout 5 วินาที output ถูก redact secret ก่อน inject
  - **`@url:<url>`** — เมื่อส่ง จะถูก rewrite เป็น `fetch <url> with web_fetch and use its content`
    (ไม่มีการ prefetch — network ยังอยู่ภายใต้ agent tool และ SSRF guard)
- **↑ / ↓** เลื่อนดู input history
- **Tab** ยืนยัน completion ที่ highlight อยู่ (`/command` หรือ `@file`) ระบบ completion
  ใช้ **fuzzy engine สองชั้น**: prefix matches ที่ตรงแน่ๆ จะแสดงก่อน (predictability เดิม)
  ตามด้วย subsequence fuzzy matches — เช่น `/cmit` หา `/commit` และ `@pyprjct` หา
  `pyproject.toml`
- **Ghost autosuggest:** ขณะพิมพ์ history entry ที่ตรงกันล่าสุดจะปรากฏเป็น ghost text สีหม่นหลัง cursor (แบบ fish/zsh) กด **→ (Right arrow)** หรือ **Ctrl+E** ที่ท้ายบรรทัดเพื่อยืนยัน suggestion เต็ม ghost จะซ่อนตัวขณะที่ completion dropdown เปิดอยู่; Right arrow ยังคงเลื่อน cursor กลางบรรทัดได้ปกติ
- **Ctrl+R** เปิด **reverse-history picker**: overlay แบบ arrow-key สำหรับ 50 history entry ล่าสุดที่ไม่ซ้ำกัน พร้อม live type-to-filter กด **↑/↓** เพื่อนำทาง, **Enter** เพื่อเติม input (ไม่ส่ง), **Esc** เพื่อยกเลิก ใช้ได้แม้ขณะ turn กำลังรัน
- **Shift+Tab** วน permission mode (plan → ask → auto-edit → yolo); mode ใหม่จะ flash ที่ input border และค้างอยู่ใน status bar
- **Ctrl+O** (หรือ **`/expand`**) เปิด tool output เต็มของ turn ล่าสุดใน pager
- **Ctrl+V** (macOS) วางรูป/screenshot จาก clipboard — จะถูกเซฟไว้ใต้ `.jarn/pastes/` และแทรกเป็น `@path` ที่ agent อ่านตอนส่ง เมื่อ `execution.inline_images: auto` (ค่าเริ่มต้น) รูปที่ `@`-mention (≤ 5 MB) จะถูกส่งให้โมเดลเป็น **image content block** โดยตรงในข้อความ — โมเดล vision ที่อ่อนจึงเห็นรูปทันทีโดยไม่ต้องเรียก `read_file` ตั้ง `inline_images: off` เพื่อกลับไปใช้แบบข้อความ `@path` ล้วน ถ้า provider ปฏิเสธรูป JARN จะลอง turn นั้นใหม่แบบ **text-only** หนึ่งครั้งและหยุด inline รูปตลอด session
- **Esc Esc** (กด Esc สองครั้งภายใน 500 ms ขณะ idle และ input ว่าง) เปิด **`/rewind` picker** — chord เดียวกับ Claude Code ครั้งแรก Esc ยังล้าง input ที่มีข้อความอยู่ตามเดิม ครั้งที่สองบน buffer ว่างจึงจะเปิด picker หลังเลือก turn จะมี confirm ด้วยลูกศรอีกครั้งให้เลือก **Restore files too** (ย้อน working tree กลับไปที่ checkpoint ของ turn นั้น พร้อม preview แบบ `git diff --stat`) หรือ **Conversation only** (คงไฟล์ไว้เหมือนเดิม) การ restore ย้อนกลับได้ด้วย `/undo` และต้องเปิด `git.autocheckpoint` (ไม่งั้น picker จะย้อนเฉพาะ conversation เหมือนเดิม)
- **Esc** ยกเลิก turn ที่กำลังรัน **Ctrl+C** ยกเลิก turn / ล้าง input และกด **สองครั้งติดกัน** เพื่อออก (แบบ Claude Code) **Ctrl+Q** ก็ออกได้
- **Copy text:** terminal เป็นเจ้าของ selection — แค่ **drag เลือกแล้ว ⌘C** (หรือปุ่ม copy ของ terminal) และเลื่อนด้วย native scrollback ของ terminal เหมือน Claude Code เป๊ะ
- **การแจ้งเตือน:** เมื่อ turn ใช้เวลานานเกิน `ui.notify_min_secs` (ค่าเริ่มต้น 10 วินาที) jarn จะส่ง **terminal bell** (`\a`) ตั้ง `ui.notify: desktop` เพื่อรับ native OS notification (macOS / Linux), `both` สำหรับ bell + desktop, หรือ `off` เพื่อปิดการแจ้งเตือนทั้งหมด (Approval prompt จะแจ้งเตือนเสมอโดยไม่คำนึงถึงเวลา)
- **ชื่อ tab terminal:** jarn อัปเดต tab title ผ่าน OSC 2 เพื่อแสดงสถานะ — `jarn — <project>` (ว่าง), `✳ jarn — <project>` (กำลังทำงาน), `⏸ jarn — <project>` (รอ approval) ตั้ง `ui.terminal_title: false` เพื่อปิดฟีเจอร์นี้
- **Checklist แผนงานแบบ live:** เมื่อ agent วางแผน จะมี checklist `⏺ Todos` แสดงเหนือ input และอัปเดต **แบบ in place** ตามที่แต่ละรายการเปลี่ยนสถานะ (✔ เสร็จ / ◐ กำลังทำ / ☐ รอ) แบบ Claude Code โดยมี reply ที่ stream อยู่ด้านล่าง แผนที่ยาวจะถูก cap (ส่วนเกินยุบเป็น `… +N more`); รายการเต็มจะถูก commit ลง scrollback เมื่อจบ turn

Reply ของ assistant render เป็น **Markdown** (heading, list, code ที่มี syntax highlight)

`/model`, `/mode`, และ `/resume` ถ้าไม่ใส่ argument จะเปิด **arrow-key picker** (↑/↓ + Enter; Esc ยกเลิก) `/model` ยังมี custom ref prompt ด้วย

ขณะที่ turn กำลังรัน บรรทัดที่ submit ไปจะถูก **queue** (แสดงใน toolbar เป็น `queue N`) จัดการด้วย `/queue`, `/queue clear`, `/queue cancel <n>`, หรือ `/queue move <from> <to>`

**Steer กลางเทิร์น (mid-turn steering).** ไม่อยากรอให้บรรทัดที่ queue ไว้รันในเทิร์นถัดไป? steer มัน **เข้าไป** ในเทิร์นที่กำลังรันได้เลย: กด **`[s]`** (steer now) บนบรรทัดที่เพิ่ง queue หรือใช้ `/queue steer <n>` เพื่อดันบรรทัดที่ _n_ ข้อความ steer จะถูก append เข้า conversation เป็นข้อความ user ใหม่ และ agent จะเห็น **ก่อน tool call ถัดไป** — เหมาะกับการแก้ทิศ agent กลาง refactor ยาว ๆ ("จริง ๆ ใช้ `pathlib` เถอะ") โดยไม่ต้องยกเลิกแล้วพิมพ์ใหม่ การ steer รัน model step ที่กำลัง in-flight ซ้ำพร้อม guidance ของคุณ (เพิ่ม model call หนึ่งครั้ง) — tool result ที่เสร็จแล้วไม่ถูกรันซ้ำ จึงไม่ทำให้ tool call ค้างกลางคัน ถ้าเทิร์นจบก่อนพอดี steer จะรันเป็นเทิร์นถัดไป (ไม่หาย) ปิดด้วย `ui.steering: false` (ซ่อนปุ่ม `[s]`; `/queue steer` จะปฏิเสธอย่างสุภาพ)

### Built-in commands (คำสั่งในตัว)

| Command | คำอธิบาย |
|---|---|
| `/help` | แสดง command และ shortcut ที่ใช้ได้ |
| `/init` | สร้างไฟล์ JARN.md สำหรับ project context |
| `/config` | ดูหรือแก้ settings: /config, /config get <key>, /config set <key> <value> (เซฟถาวร) |
| `/model [/ref\|refresh]` | ดูหรือเปลี่ยน model ที่ใช้งานอยู่; /model refresh re-query local endpoint |
| `/mode [plan\|ask\|auto-edit\|yolo]` | ดูหรือเปลี่ยน permission mode (plan/ask/auto-edit/yolo) |
| `/theme [dark\|light\|high-contrast\|auto]` | ดูหรือเปลี่ยน color theme (dark/light/high-contrast/auto) |
| `/sandbox [on\|off]` | ดูหรือ toggle execution backend (local/sandbox) |
| `/key [<key>]` | ตั้งหรือเปลี่ยน API key ของ provider ปัจจุบัน (เก็บใน keychain) |
| `/preset [<preset-name>]` | ดูหรือ apply preset — shortcut ที่ตั้ง mode + sandbox พร้อมกัน |
| `/cost` | ดู token usage และ cost ของ session |
| `/compact` | สรุปและบีบอัด conversation context |
| `/expand` | เปิด tool output เต็มของ turn ล่าสุดใน pager (เหมือน Ctrl+O) |
| `/clear` | ล้าง conversation และเริ่ม thread ใหม่ |
| `/sessions` | รายการ session เก่า พร้อมเลือก resume |
| `/resume` | เลือก session เก่าเพื่อ resume |
| `/rewind` | ย้อนไป turn ก่อนหน้าแล้วทำต่อ (fork เป็น thread ใหม่) มี confirm ที่ restore working tree กลับไปที่ checkpoint ของ turn นั้นได้ด้วย เพื่อให้ conversation กับไฟล์ย้อนพร้อมกัน |
| `/skills` | รายการ skill ที่ใช้ได้ |
| `/memory [search\|show\|add\|update\|delete\|dump] ...` | จัดการ long-term memory: list, search, show, add, update, delete, dump |
| `/permissions` | ดู permission rule และ allowlist ปัจจุบัน |
| `/mcp [status] [--refresh]` | ดู MCP server ที่ตั้งไว้ พร้อม health และ error ล่าสุดราย server |
| `/trust` | trust project root นี้ เพื่อยกเลิก review-only floor ของ repo ที่ยังไม่ trust |
| `/add-dir <path>` | เพิ่ม directory เข้า write scope ของ session นี้ (multi-root; ต้องอนุมัติ) |
| `/queue [clear\|cancel <n>\|move <from> <to>\|steer <n>]` | ดูหรือจัดการ input ที่ queue ไว้ (ขณะ turn รันอยู่) |
| `/undo` | ย้อนกลับ file change ของ agent turn ล่าสุด |
| `/redo` | ทำ file change ที่ undo ไปซ้ำอีกครั้ง |
| `/abort` | ยกเลิก turn ที่กำลังรันและ roll back file change ของมัน |
| `/commit` | ร่าง commit message จาก diff ปัจจุบันแล้ว commit (ขออนุมัติก่อน) |
| `/review` | review diff ของ working-tree ปัจจุบันหาบั๊ก/คุณภาพ (read-only) |
| `/checkpoints` | รายการ auto-checkpoint ล่าสุด |
| `/ps [kill <id>]` | ดูหรือ kill background process (จาก run_in_background) |
| `/quit` | ออกจาก J.A.R.N. |
| `/map [focus] [--refresh]` | แสดง repo map ที่ rank แล้ว (ภาพรวม codebase) |
| `/wiki [search <q>\|list]` | ค้นหาหรือแสดงรายการหน้า wiki knowledge base |
| `/doctor` | ตรวจสอบ configuration, provider, และ key |
| `/telemetry status` | ดูสถานะ telemetry opt-in และสถิติไฟล์ local sink |

## Permission modes (โหมดการอนุญาต)

| Mode | อ่านไฟล์ | เขียนไฟล์ | Shell | Network |
|---|---|---|---|---|
| `plan` | ✅ | ❌ | ❌ | ❌ |
| `ask` (ค่าเริ่มต้น) | ✅ | ถาม | ถาม | ถาม |
| `auto-edit` | ✅ | ✅ ใน scope | ถาม | ✅ *(read-only)* |
| `yolo` | ✅ | ✅ | ✅ | ✅ |

ใน mode **`plan`** agent จะค้นคว้าแบบ read-only แล้วเสนอแผนที่เป็นรูปธรรม (`exit_plan_mode`) เมื่อคุณอนุมัติ J.A.R.N. จะ escalate mode (ค่าเริ่มต้น `auto-edit`, ตั้งได้ผ่าน `plan.exit_mode`; picker ก็มี `ask` ให้เลือก) แล้วลงมือทำตามแผนใน turn เดียวกัน — ไม่ต้องสลับ mode เอง โปรเจกต์ที่ยังไม่ trust จะถูกล็อกไว้ที่ `plan`

**danger-guard** override ทุก mode: `rm -rf` (รวม `rm -r -f` / `--recursive --force`), force-push, `git reset --hard`, `mkfs`, fork bomb, การเขียนนอก scope ฯลฯ ต้องยืนยันชัดเจนเสมอ (หรือถูกบล็อกเลย) **Esc/Ctrl+C** ยกเลิก turn *และ* kill shell ที่มันเปิดไว้ ดู [docs/PERMISSIONS.md](docs/PERMISSIONS.md)

**Untrusted repos:** `.jarn/config.yaml` ของโปรเจกต์สามารถประกาศ hook, MCP server, และ provider ได้ — ความสามารถที่รันโค้ดหรืออ่าน secret ได้ J.A.R.N. จะให้คุณ **trust โปรเจกต์** ก่อนทำตามค่าเหล่านั้น (ครั้งเดียวต่อ repo) ถ้าปฏิเสธ ค่าเหล่านั้นจะถูกข้ามไป และ session ยังรันต่อได้อย่างปลอดภัย

## Configuration (การตั้งค่า)

สองระดับ ทั้งคู่เป็น YAML และ merge กัน (project override global):

```
~/.jarn/config.yaml      global: provider, key (แบบอ้างอิง), default, budget
.jarn/config.yaml        per-project: MCP server, hook, permission rule (commit ได้)
JARN.md                  per-project context, โหลดเข้า system prompt อัตโนมัติ
```

API key ถูก **อ้างอิง ไม่ inline** — ใช้ `${ENV_VAR}` หรือ `keychain:jarn/<provider>` Project config ถูกกั้นด้วย **trust prompt** (ดูด้านบน) ดูรายละเอียดทั้งหมดที่ [docs/CONFIGURATION.md](docs/CONFIGURATION.md)

ตอน startup jarn จะตรวจสอบ PyPI อย่างเงียบๆ ว่ามี release ใหม่หรือไม่ และแสดง 1 บรรทัดเบาๆ ใต้ splash เมื่อมีเวอร์ชันใหม่ (cache 24 ชม. — ข้ามอัตโนมัติเมื่อใช้ `offline` preset หรือ headless mode) ปิดได้ด้วย `updates.check: false` ใน `~/.jarn/config.yaml`

## การขยาย (Extending)

วางไฟล์ใน `~/.jarn/{skills,commands,agents}` (global) หรือ `.jarn/{...}` (project):

- **Skills** (`skills/*.md`) — ชุดความรู้/workflow ที่นำกลับมาใช้ได้ ทริกเกอร์อัตโนมัติหรือ manual
- **Commands** (`commands/*.md`) — custom `/slash` prompt template
- **Subagents** (`agents/*.md`) — agent เฉพาะทางที่ main loop delegate งานไปได้
- **Hooks** (config) — shell command ที่รันตาม lifecycle event (เช่น lint หลัง edit, test ก่อน commit)
- **MCP servers** (config) — เชื่อมต่อ external tool server (stdio หรือ HTTP)

ดู [docs/EXTENDING.md](docs/EXTENDING.md) ([quick start](docs/EXTENDING.md#quick-start-wire-skill--hook--mcp)) และ [examples/](examples/)

## เอกสาร

- [Architecture](docs/ARCHITECTURE.md) — subsystem ต่าง ๆ ประกอบกันยังไง
- [Configuration](docs/CONFIGURATION.md) — อธิบาย config key ทุกตัว
- [Permissions](docs/PERMISSIONS.md) — mode, rule, danger-guard, การขออนุมัติ
- [Extending](docs/EXTENDING.md) — skill, command, subagent, hook, MCP
- [Contributing](docs/CONTRIBUTING.md) — dev setup, test, convention
- [Roadmap](docs/ROADMAP.md) — อะไรอยู่ใน v1 / v1.x และต่อจากนั้น
- [Web UI](docs/WEB_UI.md) — แผนหลัง launch
- [Open-core](docs/OPEN_CORE.md) — licensing & business model
- [SPEC.md](SPEC.md) — design specification ต้นฉบับ

## Development

```bash
uv sync --extra dev
uv run pytest                 # 1838 tests: logic + mocked-agent + packaging gate
uv run ruff check src tests scripts   # lint
uv run mypy src/              # type-check (CI-gated)
uv run jarn doctor            # ตรวจสอบ environment (เพิ่ม --json สำหรับ machine output)
```

## License

Apache-2.0 ดู [LICENSE](LICENSE)

สร้างบน [DeepAgents](https://github.com/langchain-ai/deepagents),
[LangGraph](https://github.com/langchain-ai/langgraph), [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit),
[Rich](https://github.com/Textualize/rich), และ
[Textual](https://github.com/Textualize/textual) (onboarding wizard เท่านั้น)
