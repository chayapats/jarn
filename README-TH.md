<div align="center">

# J.A.R.N. — Just A Reliable Nerd

TUI-first coding agent harness ที่สร้างบน [DeepAgents](https://github.com/langchain-ai/deepagents)

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)

[English](README.md) · **ภาษาไทย**

</div>

---

J.A.R.N. คือ terminal coding agent ในแนวเดียวกับ Claude Code และ Codex CLI แต่สร้างเป็น harness ของตัวเองบน DeepAgents โดยวางแผนก่อนลงมือ ถามก่อนทำสิ่งที่เสี่ยง และบังคับ verification ของโปรเจกต์ได้ แทนที่จะถือว่าสำเร็จเอง Tool รันบน **host** ของคุณเป็นค่าเริ่มต้น — อ่าน [SECURITY.md](SECURITY.md) ก่อนใช้ และถือ `.jarn/config.yaml` ของโปรเจกต์เป็น untrusted จนกว่าจะตรวจแล้ว

## ความสามารถ

- **Verified completion** — `verify.gate: auto` รันคำสั่ง acceptance ที่ตรวจพบ อนุญาตให้ซ่อมแบบจำกัดรอบหนึ่งครั้ง แล้วทำให้ turn ล้มถ้ายังไม่ผ่าน (ค่าเริ่มต้น `verify.gate: suggest`)
- **Diagnostics loop** — `verify.diagnostics: auto` lint ไฟล์ที่แก้ใน turn นั้น (ruff + pyright) และ queue auto-fix ได้หนึ่งรอบ
- **Permission system** — ทุก file write และ shell command ผ่าน `plan` / `ask` / `auto-edit` / `yolo` รวมถึง allow/deny rule แบบละเอียด
- **Trust gate** — hook, MCP server และ provider override ของโปรเจกต์ถูกตัดทิ้งจนกว่าจะรัน `jarn trust`
- **Danger-guard** — `rm -rf`, force-push, `git reset --hard` และการเขียนนอก scope ต้องยืนยันหรือถูกบล็อกเสมอ รวมถึงใน YOLO
- **Sandbox** — แยกที่ระดับ OS ด้วย macOS `sandbox-exec` / Linux `bwrap` หรือ `execution.backend: docker`
- **เลือก model เอง** — 15 provider รวม ChatGPT (Codex subscription), OpenCode Go, OpenRouter, Anthropic, OpenAI, Google, Mistral, Groq, DeepSeek, Together, Fireworks, xAI, Ollama, LM Studio และ OpenAI-compatible endpoint
- **Headless และ CI** — `jarn exec` / `jarn -p`, `--json`, `--output-schema` และ [GitHub Action](action/action.yml)
- **Project context** — โหลด `JARN.md`, `AGENTS.md` หรือ `CLAUDE.md` (ไฟล์แรกที่มี) ในฐานะข้อมูล ไม่ใช่การทับ policy
- **ขยายได้** — skill, slash command, subagent, hook และ MCP จาก `~/.jarn` และ `.jarn/`
- **Checkpoint** — auto-checkpoint พร้อม `/undo`, `/redo` และ `/rewind`
- **Multi-root** — `--add-dir` (ใส่ซ้ำได้) เพิ่ม writable root; context และ undo ยังอยู่ที่ primary root

## ติดตั้ง

รองรับ macOS (Apple Silicon) และ Linux (x64 / arm64) บน Windows ให้ใช้ WSL ส่วน Intel Mac ใช้ pip/uv (ไม่มี binary npm; ตัวติดตั้ง curl จะ fallback เป็น managed Python)

**แนะนำ:**

```bash
jarn_installer_tmp=$(mktemp "${TMPDIR:-/tmp}/jarn-install.XXXXXX") && trap '[ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"' 0 HUP INT TERM && curl -fsSL 'https://raw.githubusercontent.com/chayapats/jarn/main/install.sh' -o "$jarn_installer_tmp" && sh "$jarn_installer_tmp"; jarn_install_rc=$?; [ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"; trap - 0 HUP INT TERM; if [ "$jarn_install_rc" -eq 0 ] || [ "$jarn_install_rc" -eq 10 ]; then exec "$SHELL" -l; else (exit "$jarn_install_rc"); fi
```

คำสั่งนี้ดาวน์โหลดลงไฟล์ชั่วคราวก่อน (ถ้า `curl` ล้มเหลวจะไม่ถูกรัน) จากนั้นตรวจ checksum, smoke-test และ activate release สถานะ `10` แปลว่าติดตั้งแล้ว แต่ parent shell ยังต้อง `exec "$SHELL" -l` ดู [quickstart ห้านาที](docs/QUICKSTART.md) และ [แพลตฟอร์มที่รองรับ](docs/SUPPORTED_PLATFORMS.md)

<details>
<summary>npm / pip / uv / source</summary>

**npm** — binary สำเร็จรูป ไม่ต้องมี Python (Linux x64/arm64, macOS Apple Silicon):

```bash
npm install -g jarn-cli     # ได้คำสั่ง `jarn` (ใช้ `jarn-cli` ก็ได้)
```

**pip** — ต้องการ Python 3.12+:

```bash
pip install jarn
```

**uv:**

```bash
uv tool install jarn
```

**จาก source:**

```bash
git clone https://github.com/chayapats/jarn && cd jarn
uv sync --extra dev --extra telegram
uv run jarn
```

`uv.lock` ถูก track ไว้ให้ทีมได้ dependency เวอร์ชันเดียวกัน การติดตั้งผ่าน package manager ยังเป็นของ manager นั้น ให้อัปเดตหรือถอนด้วยตัวนั้น ดู [update, rollback และ uninstall](docs/UPDATE_ROLLBACK.md)

</details>

```bash
jarn uninstall                 # แยกหมวด; เก็บข้อมูลผู้ใช้เป็นค่าเริ่มต้น
jarn uninstall --yes           # เฉพาะ executable ที่ระบบจัดการ
jarn uninstall --credentials --yes
```

ไม่แตะ `.jarn/` ระดับโปรเจกต์ และไม่ลบ Node, Python, uv หรือ Codex ที่ใช้ร่วมกับโปรแกรมอื่น

## เริ่มใช้งาน

**ChatGPT / Codex subscription** (ไม่คิดค่า OpenAI API key แยก):

```bash
jarn setup                 # เลือก “Continue with ChatGPT”
jarn auth status           # ตรวจ dependency, auth mode, plan/workspace — ไม่โชว์ token
cd your-project && jarn
```

J.A.R.N. คุยกับ Codex App Server ทางการ และไม่อ่านหรือเก็บ ChatGPT OAuth token พื้นที่ execution ของ Codex ถูกปิด คำขอใช้ tool ถูกแปลงเป็น tool call ของ J.A.R.N. เพื่อให้ permission, danger-guard, checkpoint และ `/undo` ยังคุมงาน usage แสดง token ที่ API cost `$0` แต่ยังใช้โควตาของ ChatGPT plan สำหรับ CI ที่ใช้ร่วมกันให้เลือก provider แบบ API key

**OpenRouter OAuth:**

```bash
jarn login                 # เปิดเบราว์เซอร์ → อนุญาต → เก็บ key ใน OS keychain
cd your-project && jarn
```

ตัว setup ไม่รัน OpenRouter OAuth (จะ persist key ก่อนยืนยัน) เมื่อต้องการเส้นทางนี้ให้ใช้ `jarn login`

**ตั้งค่าเอง:**

```bash
jarn setup                 # เลือก provider, อ้างอิง key, ตั้งค่า default
cd your-project
jarn init                  # สร้าง JARN.md (ไม่บังคับ)
jarn
jarn doctor                # ตรวจ config, provider, extension
```

Wizard ยังมี **OpenCode Go** (ทางลัด API key ตอนติดตั้งครั้งแรก), cloud provider อื่น และ local model (Ollama / LM Studio) ถ้ายังไม่มี config การเปิดครั้งแรกจะรัน setup ให้เอง ใน TUI ใช้ `/help` เพื่อดู slash command

<details>
<summary>Shell completions</summary>

```bash
# zsh — ครั้งเดียว แล้วรีสตาร์ท shell
jarn completions zsh > ~/.zfunc/_jarn
# ใน ~/.zshrc ถ้ายังไม่มี: fpath=(~/.zfunc $fpath) && autoload -Uz compinit && compinit

# bash
jarn completions bash > ~/.bash_completions/jarn.bash
# ใน ~/.bashrc: source ~/.bash_completions/jarn.bash

# fish
jarn completions fish > ~/.config/fish/completions/jarn.fish
```

</details>

## การใช้งาน

**Interactive:**

```bash
jarn
jarn --resume              # เลือก session เก่า
jarn --add-dir ../lib      # เพิ่ม writable root (ใส่ซ้ำได้)
```

พิมพ์แล้วกด Enter `/help` แสดงคำสั่ง Shift+Tab วน permission mode บทสนทนาอยู่ใน native scrollback ของ terminal — ไม่ใช้ alternate screen

**Headless / scripting:**

```bash
jarn exec "summarise the open TODOs"
jarn exec --json "what changed?"
jarn -p "summarise the open TODOs"                 # สัญญาเดียวกับ exec
echo "what changed?" | jarn -p -
jarn -p "do X" --json
jarn -p "do X" --model anthropic/claude-opus-4-8
jarn -p "do X" --mode auto-edit
jarn -p "do X" --cwd /path/to/project
jarn -p "extract the version" --output-schema schema.json --json
```

`--mode` (`plan` / `ask` / `auto-edit` / `yolo`) คือ flag สาธารณะ; `--permission-mode` เป็น alias ที่ซ่อนไว้ ค่าเริ่มต้น `ask` / `plan` ปฏิเสธ tool ที่ต้องอนุมัติแล้วออก non-zero — ส่ง `--mode auto-edit` หรือ `yolo` สำหรับงานที่ไม่มีคนดู Danger-guard ยังทำงานในทุก mode

เมื่อใช้ `--output-schema` object ที่ parse ได้จะแทนที่ `result` แบบข้อความในซอง `--json` ออก `0` เมื่อสำเร็จ; ออก `9` พร้อม `error.kind: "schema"` ถ้าคำตอบไม่ตรง schema; ออก `2` พร้อม `error.kind: "usage"` ถ้าอ่านไฟล์ schema ไม่ได้

**CI** — [GitHub Action](action/action.yml):

```yaml
- uses: chayapats/jarn/action@main
  with:
    prompt: "Review this diff: …"
    preset: "review-only"     # อ่านอย่างเดียว; ใช้ 'ci' เมื่อต้องการเขียนไฟล์
    api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

Outputs: `result`, `cost_usd`, `turns` — preset `ci` (ค่าเริ่มต้นของ action) ต้องมี Docker (Ubuntu runner มีให้) สำหรับ runner ที่ไม่มี Docker ให้ใช้ `preset: trusted-repo` คู่กับ `permission_mode: auto-edit` ดูตัวอย่าง [PR review](examples/github/pr-review.yml) · [issue-fix](examples/github/issue-fix.yml) และเอกสารเต็มที่ [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md)

**Telegram gateway** (ไม่บังคับ, DM สำหรับ operator คนเดียว) binary จาก npm/standalone รวมไว้แล้ว ส่วน Python ต้อง `pip install 'jarn[telegram]'` จากนั้น `jarn gateway setup` คำตอบสตรีมเป็นดราฟต์ที่ถูกรวมช่วง และปุ่มอนุมัติคงข้อความที่กำลังอ่านไว้ แล้วลบการ์ดออกจากแชทเมื่อแตะ ห้ามใส่ `gateway:` ใน `.jarn/config.yaml` ของโปรเจกต์ ดู [docs/TELEGRAM_GATEWAY.md](docs/TELEGRAM_GATEWAY.md)

## Permission modes

| Mode | อ่านไฟล์ | เขียนไฟล์ | Shell | Network |
|---|---|---|---|---|
| `plan` | อนุญาต | ปฏิเสธ | ปฏิเสธ | ปฏิเสธ |
| `ask` (ค่าเริ่มต้น) | อนุญาต | ถาม | ถาม | ถาม |
| `auto-edit` | อนุญาต | อนุญาตใน scope | ถาม | อนุญาต *(read-only)* |
| `yolo` | อนุญาต | อนุญาต | อนุญาต | อนุญาต |

ใน `plan` agent ค้นแบบ read-only แล้วเสนอแผน (`exit_plan_mode`) เมื่ออนุมัติ session จะ escalate (ค่าเริ่มต้น `auto-edit` ผ่าน `plan.exit_mode`) แล้วทำต่อใน turn เดียวกัน โปรเจกต์ที่ยังไม่ trust ถูกล็อกที่ `plan`

Danger-guard ทับทุก mode Esc/Ctrl+C ยกเลิก turn และฆ่า shell ที่เปิดไว้ รายละเอียด: [docs/PERMISSIONS.md](docs/PERMISSIONS.md)

## Configuration

สองชั้น YAML โดยโปรเจกต์ทับค่า global:

```
~/.jarn/config.yaml      provider, อ้างอิง key, default, budget
.jarn/config.yaml        MCP, hook, permission rule (commit ได้)
JARN.md                  คำแนะนำโปรเจกต์; โหลด excerpt จำกัด และอ่านเต็มเมื่อจำเป็น
```

API key ถูกอ้างอิง ไม่ inline — `${ENV_VAR}` หรือ `keychain:jarn/<provider>` provider `codex_subscription` ไม่ใช้ key ฝั่ง J.A.R.N. (`jarn auth login`) คีย์ที่ให้ความสามารถระดับโปรเจกต์ถูกกั้นด้วย trust (`jarn trust`) อ้างอิงเต็ม: [docs/CONFIGURATION.md](docs/CONFIGURATION.md)

## Extending

วางไฟล์ใน `~/.jarn/{skills,commands,agents}` หรือ `.jarn/{...}`:

- **Skills** (`skills/*.md`) — workflow ที่ใช้ซ้ำ ทริกเกอร์อัตโนมัติหรือเอง
- **Commands** (`commands/*.md`) — `/slash` prompt template ที่กำหนดเอง
- **Subagents** (`agents/*.md`) — agent เฉพาะทางที่ main loop ส่งงานต่อได้
- **Hooks** (config) — shell ตาม lifecycle event
- **MCP servers** (config) — tool server แบบ stdio หรือ HTTP

ดู [docs/EXTENDING.md](docs/EXTENDING.md) และ [examples/](examples/)

## เอกสาร

- [Quickstart](docs/QUICKSTART.md) · [Supported platforms](docs/SUPPORTED_PLATFORMS.md)
- [Configuration](docs/CONFIGURATION.md) · [Permissions](docs/PERMISSIONS.md) · [Extending](docs/EXTENDING.md)
- [Telegram gateway](docs/TELEGRAM_GATEWAY.md) · [GitHub Action](docs/GITHUB_ACTION.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md) · [Update, rollback, uninstall](docs/UPDATE_ROLLBACK.md)
- [Architecture](docs/ARCHITECTURE.md) · [Contributing](docs/CONTRIBUTING.md) · [สารบัญเอกสาร](docs/README.md)
- [SPEC.md](SPEC.md) · [CHANGELOG.md](CHANGELOG.md) · [SECURITY.md](SECURITY.md)

## Development

```bash
uv sync --extra dev --extra telegram
uv run pytest                 # 3384 tests: logic + mocked-agent + packaging gate
uv run ruff check src tests scripts
uv run mypy src/
uv run jarn doctor            # เพิ่ม --json สำหรับเครื่องอ่าน
```

## License

Apache-2.0 ดู [LICENSE](LICENSE)

สร้างบน [DeepAgents](https://github.com/langchain-ai/deepagents),
[LangGraph](https://github.com/langchain-ai/langgraph),
[prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit),
[Rich](https://github.com/Textualize/rich) และ
[Textual](https://github.com/Textualize/textual) (onboarding wizard)
