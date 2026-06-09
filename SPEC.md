# J.A.R.N. — Just A Reliable Nerd

> A TUI-first coding agent harness built on **DeepAgents**.
> Spec date: **2026-06-04** · DeepAgents target: **v0.6.7+** · Python **3.12**

---

## 1. Identity & Scope

- **แก่นหลัก:** Coding agent (แข่ง/แทน Claude Code, Codex CLI) — แก้โค้ด, รันเชลล์, จัดการ repo
- **รูปแบบ:** TUI เท่านั้นใน v1 (Web UI = อนาคต หลัง launch)
- **ปรัชญา:** "reliable" = plan → act → **verify**, โปร่งใส (markdown/diff/approval ที่คนอ่านได้), ไม่พังกลางทาง
- **ส่วนขยายอนาคต** (general assistant / domain-specific) = ทำผ่านระบบ **skills** ไม่ใช่แก้ core

---

## 2. Architecture (locked decisions)

| ด้าน | การตัดสินใจ |
|---|---|
| Agent engine | ใช้ `deepagents` library (`create_deep_agent()`) เป็นแกน — โครงแบบ **(B)** |
| วิธีพัฒนา | หยิบ prebuilt Textual TUI ของ deepagents มา**อ่านเป็นแม่แบบ (C)** แต่เขียน J.A.R.N. เป็นโปรเจกต์แยก ไม่ fork ตรงๆ |
| ภาษา/สแตก | **Python 3.12 + uv** — chat UI = `jarn.repl` (prompt_toolkit + Rich) in-process กับ agent; Textual ใช้เฉพาะ onboarding wizard (ไม่มี IPC) |
| Upstream | `deepagents` เป็น dependency ปกติ อัพตาม upstream ได้ |

---

## 3. Models & Providers

- กลยุทธ์: **Multi-provider, BYO key**, default profile = **OpenRouter** (เลือกโมเดลได้เยอะ)
- Original v1 provider target: **Anthropic · OpenAI · OpenRouter · Ollama · LM Studio**.
  The current implementation is broader — **13 providers**: OpenRouter, OpenAI,
  LM Studio, Groq, DeepSeek, Together, Fireworks, xAI, OpenAI-compatible (any
  custom `base_url`), Anthropic, Ollama, Google, Mistral (see `ProviderType`).
- **Per-task model routing**: มีตั้งแต่ออกแบบ (subagent/งานย่อยใช้โมเดลถูก, งานหลักใช้โมเดลแรง)
- Key เก็บแบบอ้างอิง **env var / OS keychain** — ไม่ hardcode ลง config

---

## 4. Execution Backend / Workspace

- **Local-first** เป็น default (อ่าน/แก้ไฟล์จริง + shell จริง) ผ่าน DeepAgents `BackendProtocol` + `LocalShellBackend`
- ออกแบบ backend ให้**สลับเป็น sandbox ได้** (มุ่งไป "เลือกได้ต่อ session" ในอนาคต)
- Workspace ถูก **scope ที่ project root (cwd)** เป็น default
- **Shell จริงตั้งแต่ v1**

---

## 5. Permissions / Human-in-the-Loop

ใช้ทั้งสองเลเยอร์ (**C**):

- **Approval modes** (หยาบ): Plan/Read-only → Ask (default) → Auto-edit → YOLO/Full-auto
- **Allowlist/Denylist rules** (ละเอียด): auto-allow/deny ต่อคำสั่ง (DeepAgents permission rules)
- **default mode = Ask** (ถามก่อน write file / shell ที่มีผล / network)
- **จำการอนุมัติ:** allow once / allow for session / always allow (เขียนลง config per-project)
- **Hard danger-guard** (override ไม่ได้ง่าย): `rm -rf`, force push, แก้ไฟล์นอก scope ⇒ ต้องยืนยันพิเศษเสมอ แม้อยู่ใน auto mode

---

## 6. Capabilities — v1 Scope

**Core (ต้องมี):**
- File read/write/edit (multi-file)
- Shell execution จริง
- Planning / `write_todos` (todo list ในจอ)
- Subagents แบบ inline (`task` tool) + **parallel subagents**

**High-value (เข้า v1 ทั้งหมด):**
- Web search + web fetch
- MCP client (stdio + http)
- Skills
- Git awareness (diff/branch/status + HITL commit)
- Long-term memory (markdown-first)

**Shipped (post-v1, on main):** OS-level execution sandbox (`execution.local_sandbox`) · Repo map (`context.repo_map` / `/map`) · Wiki knowledge base (`wiki.enabled` / `/wiki`) · Auto-checkpoint + `/undo`/`/redo` · AGENTS.md/CLAUDE.md interop · Headless one-shot (`jarn -p`) · JSONL session transcript · `!` shell escape

**Later (v2+):** Web UI · Open-core hosted sandbox / cloud features

---

## 7. Memory & Persistence (3 ชั้น)

1. **Thread/conversation state** → **SQLite local** checkpointer (resume session, ดูประวัติ)
2. **Long-term memory** → **markdown-first** (โปร่งใส คนแก้ได้) เก็บ preference/ข้อเท็จจริงโปรเจกต์/feedback/decisions → เสริม vector recall ใน v1.x
3. **Project context** → **`JARN.md`** auto-load เข้า system prompt + `/init` สร้างให้

---

## 8. Extensibility (5 พื้นผิว — เข้า v1 ทั้งหมด)

1. **Slash commands** — built-in set + custom (ไฟล์ `.md`) ใน `commands/`
2. **Skills** — `SKILL.md` + YAML frontmatter (name/description/trigger) ใน `skills/`
3. **Hooks** — รันคำสั่งอัตโนมัติตอน event (pre/post tool, session start/stop, file change) — เช่น lint หลัง edit, test ก่อน commit
4. **MCP servers** — list ใน config (stdio + http) + per-project override
5. **Custom subagents** — ไฟล์ `.md` ใน `agents/` (ชื่อ/prompt/tools/model) ผูกกับ per-task routing

**Config format: YAML**

---

## 9. TUI / UX

- **เลย์เอาต์:** Single-stream ใน native scrollback (Claude Code-style); input ติดล่าง;
  todos inline หลัง turn; `/resume` picker; toolbar แสดง model · mode · queue · ctx · cost
- **การแสดงผล (v1 ครบ):**
  - Streaming token real-time + thinking line ระหว่างรอ
  - **Diff view** สี ก่อนยืนยัน edit
  - **Inline todos** หลังแต่ละ turn (de-duped)
  - **Approval prompt** inline (allow once/session/always) + arrow-key menu
  - Tool results สรุปบรรทัดเดียว + **Ctrl+O / `/expand`** เปิด full output
  - Adaptive **toolbar** (model · mode · queue · ctx · cost)
- **Input:** multiline editor, history (↑/↓), `/` command palette, `@` อ้างอิงไฟล์, paste, **`Esc` ยกเลิกงาน (graceful cancel + cleanup)**
- **Theme/Brand:** ASCII logo ตอนเปิด + theme light/dark + accessible

---

## 10. Reliability & Observability

- **Context management:** auto-summarize เมื่อใกล้เต็ม + แสดง context usage + `/compact`
- **Model fallback & retry:** fallback chain (ผ่าน OpenRouter), จัดการ rate-limit/timeout graceful, ไม่ crash ทั้ง session
- **Cost & token:** นับสด + `/cost` + **budget limit ต่อ session (เตือน + hard stop ตั้งค่าได้)**
- **Cancellation safety:** graceful cancel, ไม่ทิ้ง shell ค้าง/ไฟล์เขียนครึ่ง
- **Self-verification loop:** หลังแก้โค้ด → build/test/lint (ถ้ามี) ก่อนสรุปว่าเสร็จ (ผูก hooks + system prompt "plan→act→verify")
- **Observability:** LangSmith tracing (opt-in) + local structured logs (`~/.jarn/logs/`)
- **Telemetry:** opt-in, **default ปิด**, เปิดจริงตอนใกล้ launch

---

## 11. Git Behavior

- **Commit ผ่าน HITL เสมอ** (เห็น diff + ข้อความก่อนยืนยัน)
- **ห้าม auto-push** (default)
- ทำงานบน branch ปัจจุบัน — **ไม่สลับ branch เอง** เว้นสั่ง
- Force push = danger-guard (ยืนยันพิเศษ)

---

## 12. Onboarding & Distribution

- **First-run wizard:** ถาม provider/วาง API key → เลือก default model → เลือก permission mode → สร้าง `~/.jarn/config.yaml` → **ตรวจ key ว่าใช้ได้จริง**
- **แจกจ่าย v1:** `uv tool install jarn` / publish PyPI (binary มาทีหลังตอนใกล้ launch)
- **License/โมเดล:** private ตอนนี้ (เขียนแบบ license-clean, พร้อมเปิด) → มุ่ง **open-core** ตอน launch (core ฟรี + Web UI/cloud พรีเมียม)
- **แพลตฟอร์ม v1:** macOS + Linux (Windows = WSL)

---

## 13. File Layout

```
~/.jarn/                  # global
  config.yaml             # providers, key refs, default model, modes, budget
  skills/  commands/  agents/
  memory/                 # long-term markdown
  logs/                   # structured logs
.jarn/                    # per-project (commit ได้)
  config.yaml             # override, MCP servers, hooks, allowlist
  skills/  commands/  agents/
  state.sqlite            # thread checkpointer (gitignore)
JARN.md                   # project context (commit ได้)
```

---

## 14. Milestone Plan (target ≈ 1 สัปดาห์)

> ⚠️ **หมายเหตุความเสี่ยง:** scope v1 (Core + High-value ครบ + 5 extensibility surfaces + reliability ครบ) ใหญ่กว่ากรอบ 1 สัปดาห์มาตรฐานพอควร แนะนำตั้ง "1 สัปดาห์ = MVP ใช้งานได้จริง (วันที่ 1–5)" แล้ว High-value บางตัว (vector memory, telemetry) เลื่อนเป็น v1.x ถ้าเวลาไม่พอ

| วัน | เป้าหมาย |
|---|---|
| **D1** | สเกลโปรเจกต์ (uv), เชื่อม `create_deep_agent()`, provider/model config (OpenRouter default + BYO key), onboarding wizard ขั้นต่ำ |
| **D2** | Terminal REPL โครง (`repl.py`): single-stream + streaming, input (`/` `@` multiline, Esc-cancel), tool-call log *(เดิมวาง Textual full-screen — retired 2026-06-05)* |
| **D3** | Local backend + shell, **permission modes + rules + diff view + approval prompt + danger-guard** |
| **D4** | Planning/todo panel, **subagents (inline + parallel)** + per-task routing, context mgmt + `/compact` |
| **D5** | Git awareness + HITL commit, self-verification loop + hooks, `JARN.md` + `/init`, SQLite resume |
| **D6** | Web search/fetch, MCP client, skills/commands/custom-agents loaders, cost/budget + `/cost` |
| **D7** | Long-term memory (markdown), LangSmith opt-in + local logs, polish/theme, PyPI packaging |

**Deferred → v1.x / v2:** vector recall · telemetry · binary build · async/remote subagents · multimodal · Web UI

> **Note (2026-06-09):** sandbox backend, async/remote subagents, multimodal filesystem (v1.x), then OS-level execution sandbox, repo map, wiki, auto-checkpoint, AGENTS.md interop, headless mode, and JSONL transcripts all shipped in **v0.2.0**. **v0.3.0 (prepared, Alpha)** adds a Docker container backend + hardening, policy profiles + an untrusted floor, a smoke-eval harness, and `/mcp` / `/trust`. See ROADMAP.md.

---

## 15. Branding, Skills Trigger & Testing (resolved)

**CLI entry-point:** `jarn` (เป็นทั้งคำสั่งและชื่อ PyPI package; เช็กชื่อว่างตอน D7, ถ้าไม่ว่าง fallback package name แต่คำสั่งยังเป็น `jarn`)

**Branding:**
- Accent หลัก = **cyan/teal** ("calm reliable") — ไม่ชนกับ Claude (ส้ม)/tool อื่น
- Splash = ASCII wordmark "J.A.R.N." + tagline "just a reliable nerd"
- Theme: light / dark / high-contrast (เปลี่ยนได้)

**Skills trigger = Hybrid:**
- frontmatter มีทั้ง `description` (auto, model-decided) และ `trigger` (keyword/pattern หรือ `manual`)
- `trigger: manual` ⇒ เรียกด้วย `/` เท่านั้น (สำหรับ skill เสี่ยง/มีผลข้างเคียง)
- explicit `/skill-name` เรียกได้เสมอ; ปิด auto ได้ใน strict/Plan mode
- danger-guard ครอบ skill ที่รัน shell/แก้ไฟล์

**Testing strategy:**
- **CI ทุก commit (บังคับ):**
  - (1) Unit/logic — `pytest`: config loader, **permission engine, allowlist, danger-guard**, model routing, cost calc
  - (2) Agent integration (mocked LLM) — agent loop/tools/HITL/verify loop/subagent routing โดย mock model (ไม่เปลือง token, ไม่ flaky)
  - (3) Front-end — `tests/test_repl.py` (headless REPL) + `tests/test_ux.py` (onboarding wizard pilot) + `tests/test_phase3.py` (registry/queue/toolbar parity); Textual chat snapshot retired
  - Gate ปัจจุบัน: **775 tests**, `ruff check src tests`, `mypy src/` = 0 errors
- **Nightly/manual:** (4) E2E live LLM บน fixture repo (smoke suite เล็ก, ไม่บล็อก CI หลัก)
- **Coverage สูงสุดที่:** permission engine + danger-guard + verify loop (หัวใจ "reliable")
