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

J.A.R.N. คือ terminal coding agent ที่ออกแบบในแนวทางเดียวกับ Claude Code และ Codex CLI แต่สร้างขึ้นเป็น harness ของตัวเองบน DeepAgents library จุดเด่นที่แตกต่างคือ **ความน่าเชื่อถือ (reliability)**: agent จะวางแผนก่อนลงมือ, ตรวจสอบผลลัพธ์ของตัวเอง, ขอ permission ก่อนทำทุกอย่างที่มีความเสี่ยง และไม่อ้างว่าทำสำเร็จแบบเดา

รันทั้งหมดใน terminal ของคุณ (Web UI อยู่ใน roadmap หลัง launch) ความสามารถเด่นได้แก่: **AGENTS.md / CLAUDE.md interop** (ทำงานร่วมกับ agent อื่นได้ทันที), **headless one-shot mode** (`jarn -p "..."`), **JSONL session transcript**, **`!` shell escape**, **OS-level execution sandbox** (macOS `sandbox-exec` / Linux `bwrap`) และ **Docker container backend** (`execution.backend: docker`), **presets** (`/preset`, `jarn --preset`) ที่ตั้ง mode + sandbox พร้อมกันในคำสั่งเดียวพร้อม untrusted floor, **auto-checkpoint + `/undo` / `/redo`**, **repo map** (`/map`), **wiki knowledge base** (`/wiki`), **`/config` settings panel** (UI แบบ tab โต้ตอบได้ เซฟลง `~/.jarn/config.yaml`), และ **MCP health** ราย server (`/mcp status`)

> **สถานะ:** v0.4.0 (Alpha) — ปล่อยบน PyPI แล้ว (`pip install jarn`) ปิดช่องว่าง 5 จุดที่ผู้ใช้เจอเทียบกับ harness อื่น (prompt caching, plan-mode handoff, `/commit` + `/review`, background processes, macOS image paste) พร้อมรอบขัดเกลา UX (live in-place streaming, conversation `/rewind`, rich `@`-mentions, in-session `/key`, อนุมัติเร็วขึ้น) ตัว architecture, configuration, permission engine และ terminal REPL พัฒนาเสร็จและผ่านการทดสอบแล้ว การเรียก model จริงต้องมี API key ของตัวเอง ดู [CHANGELOG.md](CHANGELOG.md) และ [SECURITY.md](SECURITY.md)

> **ความปลอดภัย:** J.A.R.N. รัน tool บน **host** ของคุณโดยตรงเป็นค่าเริ่มต้น (filesystem + shell จริง) `.jarn/config.yaml` ของโปรเจกต์สามารถประกาศ hook, MCP server, และ provider override ได้ — ควร trust เฉพาะ repository ที่คุณยอมรันโค้ดจากมันเท่านั้น โปรเจกต์ที่ยังไม่ trust จะถูกกั้นไว้จนกว่าคุณจะอนุมัติ (`jarn trust`) โปรดอ่าน [SECURITY.md](SECURITY.md) ก่อนใช้งาน

## ทำไมต้องเลือก J.A.R.N.?

- **Reliable by design** — flow แบบ plan → act → verify ฝังอยู่ใน system prompt พร้อม self-verification loop ที่รัน build/test/lint ของโปรเจกต์ก่อนรายงานว่าเสร็จ
- **ปลอดภัยเป็นค่าเริ่มต้น** — ระบบ permission หลายชั้น (coarse mode + fine-grained rules) คั่นกลางทุก file write และ shell command โดยมี *danger-guard* ที่ยืนยันการกระทำร้ายแรงเสมอ — แม้แต่ใน YOLO mode
- **เลือก model เองได้ (Bring your own model)** — รองรับ 13 provider (OpenRouter, Anthropic, OpenAI, Google, Mistral, Groq, DeepSeek, Together, Fireworks, xAI, Ollama, LM Studio, และ generic OpenAI-compatible endpoint) พร้อม per-task routing ให้ subagent ใช้ model ที่ถูกกว่าได้
- **รู้ต้นทุนและ context ตลอดเวลา** — ติดตาม token/cost แบบ live (พร้อม breakdown ราย tool) และ budget ต่อ session ที่แจ้งเตือนหรือหยุดอัตโนมัติได้; มี context-% gauge และ throughput การ generate แบบ live (tok/s) ที่ทำงานกับ local model (LM Studio / Ollama) ด้วย ไม่ใช่แค่ cloud model ที่มีราคา
- **รู้วันเวลา (Date-aware)** — วันที่/เวลาท้องถิ่นปัจจุบันถูกใส่เข้า system prompt ทำให้คำสั่งที่อ้างอิง "วันนี้" ไม่ยึดติดกับ training cutoff ของ model
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

## เริ่มใช้งาน (Quick Start)

```bash
jarn setup        # wizard แรกเริ่ม: เลือก provider, เก็บ API key, ตั้งค่า default
cd your-project
jarn init         # สร้างไฟล์ JARN.md สำหรับ project context (ไม่บังคับ แต่แนะนำ)
jarn              # เปิด TUI
jarn doctor       # ตรวจสอบ config / provider / key / extension ได้ตลอดเวลา
```

ถ้ายังไม่มี config เลย J.A.R.N. จะรัน setup wizard ให้อัตโนมัติในการเปิดครั้งแรก

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

## หน้าตา interface: native inline

```bash
jarn            # เริ่ม session ใหม่
jarn --resume   # เลือก session เก่าที่ต้องการต่อตอน launch
```

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
- ขึ้นต้นบรรทัดด้วย **`/`** เพื่อใช้ command (ดูด้านล่าง) ใช้ **`@`** อ้างอิง file path
- **↑ / ↓** เลื่อนดู input history
- **Tab** ยืนยัน completion ที่ highlight อยู่ (`/command` หรือ `@file`)
- **Shift+Tab** วน permission mode (plan → ask → auto-edit → yolo); mode ใหม่จะ flash ที่ input border และค้างอยู่ใน status bar
- **Ctrl+O** (หรือ **`/expand`**) เปิด tool output เต็มของ turn ล่าสุดใน pager
- **Ctrl+V** (macOS) วางรูป/screenshot จาก clipboard — จะถูกเซฟไว้ใต้ `.jarn/pastes/` และแทรกเป็น `@path` ที่ agent อ่านตอนส่ง
- **Esc** ยกเลิก turn ที่กำลังรัน **Ctrl+C** ยกเลิก turn / ล้าง input และกด **สองครั้งติดกัน** เพื่อออก (แบบ Claude Code) **Ctrl+Q** ก็ออกได้
- **Copy text:** terminal เป็นเจ้าของ selection — แค่ **drag เลือกแล้ว ⌘C** (หรือปุ่ม copy ของ terminal) และเลื่อนด้วย native scrollback ของ terminal เหมือน Claude Code เป๊ะ

Reply ของ assistant render เป็น **Markdown** (heading, list, code ที่มี syntax highlight)

`/model`, `/mode`, และ `/resume` ถ้าไม่ใส่ argument จะเปิด **arrow-key picker** (↑/↓ + Enter; Esc ยกเลิก) `/model` ยังมี custom ref prompt ด้วย

ขณะที่ turn กำลังรัน บรรทัดที่ submit ไปจะถูก **queue** (แสดงใน toolbar เป็น `queue N`) จัดการด้วย `/queue`, `/queue clear`, `/queue cancel <n>`, หรือ `/queue move <from> <to>`

### Built-in commands (คำสั่งในตัว)

| Command | คำอธิบาย |
|---|---|
| `/help` | แสดง command และ shortcut ที่ใช้ได้ |
| `/init` | สร้างไฟล์ JARN.md สำหรับ project context |
| `/config` | ดูหรือแก้ settings: /config, /config get <key>, /config set <key> <value> (เซฟถาวร) |
| `/model [/ref\|refresh]` | ดูหรือเปลี่ยน model ที่ใช้งานอยู่; /model refresh re-query local endpoint |
| `/mode [plan\|ask\|auto-edit\|yolo]` | ดูหรือเปลี่ยน permission mode (plan/ask/auto-edit/yolo) |
| `/sandbox [on\|off]` | ดูหรือ toggle execution backend (local/sandbox) |
| `/key [<key>]` | ตั้งหรือเปลี่ยน API key ของ provider ปัจจุบัน (เก็บใน keychain) |
| `/preset [<preset-name>]` | ดูหรือ apply preset — shortcut ที่ตั้ง mode + sandbox พร้อมกัน |
| `/profile [<preset-name>]` | alias เก่าของ /preset (ยังใช้ได้) |
| `/cost` | ดู token usage และ cost ของ session |
| `/compact` | สรุปและบีบอัด conversation context |
| `/expand` | เปิด tool output เต็มของ turn ล่าสุดใน pager (เหมือน Ctrl+O) |
| `/clear` | ล้าง conversation และเริ่ม thread ใหม่ |
| `/sessions` | รายการ session เก่า พร้อมเลือก resume |
| `/resume` | เลือก session เก่าเพื่อ resume |
| `/rewind` | ย้อน conversation ไป turn ก่อนหน้าแล้วทำต่อ (fork เป็น thread ใหม่) |
| `/skills` | รายการ skill ที่ใช้ได้ |
| `/memory [search\|show\|add\|update\|delete\|dump] ...` | จัดการ long-term memory: list, search, show, add, update, delete, dump |
| `/permissions` | ดู permission rule และ allowlist ปัจจุบัน |
| `/mcp [status]` | ดู MCP server ที่ตั้งไว้ พร้อม health และ error ล่าสุดราย server |
| `/trust` | trust project root นี้ เพื่อยกเลิก review-only floor ของ repo ที่ยังไม่ trust |
| `/queue [clear\|cancel <n>\|move <from> <to>]` | ดูหรือจัดการ input ที่ queue ไว้ (ขณะ turn รันอยู่) |
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
uv run pytest                 # 1166 tests: logic + mocked-agent + packaging gate
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
