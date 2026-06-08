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

</div>

---

J.A.R.N. คือ terminal coding agent ที่ออกแบบในแนวทางเดียวกับ Claude Code และ Codex CLI แต่สร้างขึ้นเป็น harness ของตัวเองบน DeepAgents จุดเด่นที่แตกต่างคือ **ความน่าเชื่อถือ (reliability)**: agent จะวางแผนก่อนลงมือ, ตรวจสอบผลลัพธ์ของตัวเอง, และขอ permission ก่อนทำทุกอย่างที่มีความเสี่ยง ไม่มีการอ้างว่าทำสำเร็จแบบเดา

รันทั้งหมดใน terminal (Web UI อยู่ใน roadmap) ความสามารถหลักได้แก่: **AGENTS.md / CLAUDE.md interop** (ทำงานร่วมกับ agent อื่นได้ทันที), **headless one-shot mode** (`jarn -p "..."`), **JSONL session transcript**, **`!` shell escape**, **OS-level execution sandbox** (macOS `sandbox-exec` / Linux `bwrap`), **auto-checkpoint + `/undo` / `/redo`**, **repo map** (`/map`), และ **wiki knowledge base** (`/wiki`)

> **สถานะ:** v0.2.0 บน PyPI — architecture, configuration, permission engine และ terminal REPL พร้อมใช้งานและผ่านการทดสอบแล้ว การเรียก model จริงต้องมี API key ของตัวเอง ดู [CHANGELOG.md](CHANGELOG.md) และ [SECURITY.md](SECURITY.md)

> **ความปลอดภัย:** J.A.R.N. รัน tool บน **host** ของคุณโดยตรง (filesystem + shell จริง) `.jarn/config.yaml` ของโปรเจกต์สามารถกำหนด hook, MCP server, และ provider ได้ — ควร trust เฉพาะ repository ที่คุณไว้วางใจ โปรดอ่าน [SECURITY.md](SECURITY.md) ก่อนใช้งาน

## ทำไมต้องเลือก J.A.R.N.?

- **Reliable by design** — flow แบบ plan → act → verify ฝังอยู่ใน system prompt มี self-verification loop ที่รัน build/test/lint ของโปรเจกต์ก่อนรายงานว่าเสร็จ
- **ปลอดภัยเป็นค่าเริ่มต้น** — ระบบ permission หลายชั้น (coarse mode + fine-grained rules) คั่นกลางทุก file write และ shell command มี *danger-guard* ที่ยืนยันการกระทำอันตราย — แม้แต่ใน YOLO mode
- **เลือก model เองได้** — รองรับ 13 provider (OpenRouter, Anthropic, OpenAI, Google, Mistral, Groq, DeepSeek, Together, Fireworks, xAI, Ollama, LM Studio, และ generic OpenAI-compatible endpoint) พร้อม per-task routing ให้ subagent ใช้ model ที่ถูกกว่าได้
- **รู้ต้นทุนตลอดเวลา** — ติดตาม token/cost แบบ live มี budget ต่อ session ที่แจ้งเตือนหรือหยุดอัตโนมัติได้
- **ขยายได้ง่าย** — skill, slash command, custom subagent, lifecycle hook, และ MCP server กำหนดผ่านไฟล์ธรรมดาใน `~/.jarn` และ `.jarn/`

## ติดตั้ง

ต้องการ **Python 3.12+** และ [uv](https://docs.astral.sh/uv/) รองรับ macOS และ Linux (Windows ผ่าน WSL)

```bash
pip install jarn            # ติดตั้งจาก PyPI
# หรือ: uv tool install jarn

# ติดตั้งจาก source:
git clone https://github.com/chayapats/jarn && cd jarn
uv sync --extra dev
uv run jarn
```

`uv.lock` อยู่ใน repo เพื่อให้ทุกคนในทีมได้ dependency เวอร์ชันเดียวกัน

### แชร์กับทีม

```bash
git clone <repo-url> && cd jarn
uv sync --extra dev
uv run jarn setup          # ทำครั้งเดียวต่อเครื่อง — เก็บ API key ใน ~/.jarn
cd your-project
jarn doctor                # ตรวจสอบ config, provider, และ extension ที่โหลดอยู่
jarn                       # จะถามให้ trust ถ้าโปรเจกต์มี hook/MCP
jarn trust .               # pre-approve repo ที่คุณควบคุมเอง (ไม่บังคับ)
```

ถ้า repo ที่ clone มามี `.jarn/config.yaml` ที่กำหนด hook, MCP server หรือ provider override J.A.R.N. จะถามก่อนทำตาม คุณสามารถปฏิเสธแล้วรันต่อโดยปลอดภัย หรือรัน `jarn trust <path>` หลังตรวจสอบ repo แล้ว ใช้ `jarn doctor` เพื่อดูว่า skill, command, subagent, hook, และ MCP server ใดจะถูกโหลด

## เริ่มใช้งาน (Quick Start)

```bash
jarn setup        # wizard แรกเริ่ม: เลือก provider, เก็บ API key, ตั้งค่า default
cd your-project
jarn init         # สร้างไฟล์ JARN.md สำหรับ project context (แนะนำให้ทำ)
jarn              # เปิด TUI
jarn doctor       # ตรวจสอบ config / provider / key / extension ได้ตลอด
```

ถ้ายังไม่มี config เลย J.A.R.N. จะรัน setup wizard ให้อัตโนมัติในการเปิดครั้งแรก

## Non-interactive / scripting (โหมด headless)

ใช้ flag `-p` เพื่อรันแบบ one-shot แล้วออกพร้อม output:

```bash
jarn -p "summarise the open TODOs"          # พิมพ์คำตอบแล้วออก
echo "what changed?" | jarn -p -            # อ่าน prompt จาก stdin
jarn -p "do X" --json                        # output เป็น JSON: {result, tokens, cost, turns}
jarn -p "do X" --model anthropic/claude-opus-4-8  # override model สำหรับรันนี้
jarn -p "do X" --permission-mode auto-edit  # อนุญาต file write โดยไม่ถาม
jarn -p "do X" --cwd /path/to/project       # กำหนด working directory
```

**Fail-closed safety:** mode เริ่มต้น (`ask` / `plan`) จะปฏิเสธ tool ที่ปกติต้องขออนุมัติ และออกด้วย non-zero exit code ถ้าต้องการให้ทำงานโดยไม่มีคนดู ให้ใช้ `--permission-mode auto-edit` หรือ `yolo` — danger-guard ยังบล็อกคำสั่งอันตรายทุก mode อยู่

## หน้าตา TUI (native inline)

```bash
jarn            # เริ่ม session ใหม่
jarn --resume   # เลือก session เก่าที่ต้องการต่อตอน launch
```

J.A.R.N. แสดงผล conversation ลงบน terminal buffer ปกติ — ไม่มี alternate screen conversation ทั้งหมดอยู่ใน **native scrollback** ของ terminal: เลื่อนดูด้วย scroll เดียว และ copy ได้ตามปกติ เหมือน Claude Code reply ของ assistant จะ stream แบบ live และ render เป็น Markdown, tool call, การขออนุมัติ, และ diff preview ต่อ turn แสดงแบบ inline

```
┌ toolbar: model · mode · queue · ctx · cost ─────────────────────────────┐
│                                                                            │
│   conversation stream (assistant output, tool calls, approvals)           │
│                                                                            │
├────────────────────────────────────────────────────────────────────────┤
│ › your message…                                                          │
└────────────────────────────────────────────────────────────────────────┘
```

## วิธีใช้งาน

- **พิมพ์** message แล้วกด **Enter** เพื่อส่ง (**Shift+Enter** / **Ctrl+J** สำหรับขึ้นบรรทัดใหม่)
- ขึ้นต้นด้วย **`/`** เพื่อใช้ command (ดูตารางด้านล่าง) ใช้ **`@`** อ้างอิง file path
- **↑ / ↓** เลื่อนดู input history
- **Tab** ยืนยัน completion ที่ highlight อยู่ (`/command` หรือ `@file`)
- **Shift+Tab** วน permission mode (plan → ask → auto-edit → yolo); mode ใหม่จะแสดงที่ input border และ status bar
- **Ctrl+O** (หรือ **`/expand`**) เปิด tool output ของ turn ล่าสุดใน pager
- **Esc** ยกเลิก turn ที่กำลังรัน **Ctrl+C** ยกเลิก turn / ล้าง input และกด **สองครั้งติดกัน** เพื่อออก (เหมือน Claude Code) **Ctrl+Q** ออกได้เช่นกัน
- **Copy text:** terminal จัดการ selection — **drag เลือกแล้ว ⌘C** (หรือปุ่ม copy ของ terminal) แล้วเลื่อนด้วย native scrollback เหมือน Claude Code

Reply ของ assistant render เป็น **Markdown** (heading, list, code ที่มี syntax highlight)

`/model`, `/mode`, และ `/resume` ถ้าไม่ใส่ argument จะเปิด **arrow-key picker** (↑/↓ + Enter; Esc ยกเลิก) `/model` ยังมี custom ref prompt ด้วย

ขณะที่ turn กำลังรัน บรรทัดที่ submit ไปจะถูก **queue** (แสดงใน toolbar เป็น `queue N`) จัดการด้วย `/queue`, `/queue clear`, `/queue cancel <n>`, หรือ `/queue move <from> <to>`

### `!` Shell escape

พิมพ์ `!` นำหน้าเพื่อรันคำสั่ง shell โดยตรงจาก input เช่น:

```
! git status
! ls -la src/
```

output จะแสดงแบบ inline โดยไม่ต้องออกจาก J.A.R.N.

### Built-in commands (คำสั่งในตัว)

| Command | คำอธิบาย |
|---|---|
| `/help` | แสดง command และ shortcut ที่ใช้ได้ |
| `/init` | สร้างไฟล์ JARN.md สำหรับ project context |
| `/model [/ref]` | ดูหรือเปลี่ยน model ที่ใช้งานอยู่ |
| `/mode [plan\|ask\|auto-edit\|yolo]` | ดูหรือเปลี่ยน permission mode |
| `/sandbox [on\|off]` | ดูหรือ toggle execution backend (local/sandbox) |
| `/cost` | ดู token usage และ cost ของ session |
| `/compact` | สรุปและบีบอัด conversation context |
| `/expand` | เปิด tool output ของ turn ล่าสุดใน pager (เหมือน Ctrl+O) |
| `/clear` | ล้าง conversation และเริ่ม thread ใหม่ |
| `/sessions` | รายการ session เก่า พร้อมเลือก resume |
| `/resume` | เลือก session เก่าเพื่อ resume |
| `/skills` | รายการ skill ที่ใช้ได้ |
| `/memory [search\|show\|add\|update\|delete] ...` | จัดการ long-term memory |
| `/permissions` | ดู permission rule และ allowlist ปัจจุบัน |
| `/queue [clear\|cancel <n>\|move <from> <to>]` | ดูหรือจัดการ queue ขณะ turn รันอยู่ |
| `/undo` | ย้อนกลับ file change ของ agent turn ล่าสุด |
| `/redo` | ทำ file change ที่ undo ไปซ้ำอีกครั้ง |
| `/checkpoints` | รายการ auto-checkpoint ล่าสุด |
| `/quit` | ออกจาก J.A.R.N. |
| `/map [focus] [--refresh]` | แสดง repo map (ภาพรวม codebase) |
| `/wiki [search <q>\|list]` | ค้นหาหรือแสดงรายการหน้า wiki knowledge base |

## Permission modes (โหมดการอนุญาต)

Permission mode กำหนดว่า agent ทำอะไรได้บ้างโดยไม่ต้องถามก่อน:

| Mode | อ่านไฟล์ | เขียนไฟล์ | Shell | Network |
|---|---|---|---|---|
| `plan` | ✅ | ❌ | ❌ | ❌ |
| `ask` (ค่าเริ่มต้น) | ✅ | ถาม | ถาม | ถาม |
| `auto-edit` | ✅ | ✅ ใน scope | ถาม | ✅ *(read-only)* |
| `yolo` | ✅ | ✅ | ✅ | ✅ |

**danger-guard** override ทุก mode: `rm -rf`, force-push, `git reset --hard`, `mkfs`, fork bomb, การเขียนนอก scope ฯลฯ — ต้องยืนยันเสมอ (หรือบล็อกเลย) **Esc/Ctrl+C** ยกเลิก turn *และ* kill shell ที่รันอยู่ ดูรายละเอียดที่ [docs/PERMISSIONS.md](docs/PERMISSIONS.md)

**Untrusted repos:** `.jarn/config.yaml` ของโปรเจกต์สามารถกำหนด hook, MCP server, และ provider ได้ — ซึ่งสามารถรัน code หรืออ่าน secret ได้ J.A.R.N. จะถามให้ **trust โปรเจกต์** ก่อนทำตาม (ครั้งเดียวต่อ repo) ถ้าปฏิเสธ ส่วนนั้นจะถูกข้ามไปและ session ยังรันต่อได้อย่างปลอดภัย

## Configuration (การตั้งค่า)

สองระดับ ทั้งคู่เป็น YAML และ merge กัน (project override global):

```
~/.jarn/config.yaml      global: provider, key, default, budget
.jarn/config.yaml        per-project: MCP server, hook, permission rule (commit ได้)
JARN.md                  project context, โหลดเข้า system prompt อัตโนมัติ
```

API key ถูก **อ้างอิง ไม่ inline** — ใช้ `${ENV_VAR}` หรือ `keychain:jarn/<provider>` Project config ถูกกั้นด้วย **trust prompt** ดูรายละเอียดทั้งหมดที่ [docs/CONFIGURATION.md](docs/CONFIGURATION.md)

## การขยาย (Extending)

วางไฟล์ใน `~/.jarn/{skills,commands,agents}` (global) หรือ `.jarn/{...}` (project):

- **Skills** (`skills/*.md`) — ชุดความรู้/workflow ที่นำกลับมาใช้ได้ ทริกเกอร์อัตโนมัติหรือ manual
- **Commands** (`commands/*.md`) — custom `/slash` prompt template
- **Subagents** (`agents/*.md`) — agent เฉพาะทางที่ main loop delegate งานไปได้
- **Hooks** (config) — shell command ที่รันตาม lifecycle event (เช่น lint หลัง edit, test ก่อน commit)
- **MCP servers** (config) — เชื่อมต่อ external tool server (stdio หรือ HTTP)

ดู [docs/EXTENDING.md](docs/EXTENDING.md) ([quick start](docs/EXTENDING.md#quick-start-wire-skill--hook--mcp)) และ [examples/](examples/)

## เอกสาร

- [Architecture](docs/ARCHITECTURE.md) — ภาพรวม subsystem ทั้งหมด
- [Configuration](docs/CONFIGURATION.md) — อธิบาย config key ทุกตัว
- [Permissions](docs/PERMISSIONS.md) — mode, rule, danger-guard, การขออนุมัติ
- [Extending](docs/EXTENDING.md) — skill, command, subagent, hook, MCP
- [Contributing](docs/CONTRIBUTING.md) — dev setup, test, convention
- [Roadmap](docs/ROADMAP.md) — v1 / v1.x และต่อจากนั้น
- [Web UI](docs/WEB_UI.md) — แผนหลังจาก launch
- [Open-core](docs/OPEN_CORE.md) — licensing & business model
- [SPEC.md](SPEC.md) — original design specification

## Development

```bash
uv sync --extra dev
uv run pytest                 # 602 tests: logic + mocked-agent + packaging gate
uv run ruff check src tests   # lint
uv run mypy src/              # type-check (CI-gated)
uv run jarn doctor            # ตรวจสอบ environment (เพิ่ม --json สำหรับ machine output)
```

## License

Apache-2.0 ดู [LICENSE](LICENSE)

สร้างบน [DeepAgents](https://github.com/langchain-ai/deepagents),
[LangGraph](https://github.com/langchain-ai/langgraph), [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit),
[Rich](https://github.com/Textualize/rich), และ
[Textual](https://github.com/Textualize/textual) (onboarding wizard เท่านั้น)
