"""Shipped defaults and the template written by the onboarding wizard.

These keep the model identifiers in one place (current as of June 2026) so they
can be refreshed without touching loader logic.
"""

from __future__ import annotations

# Current flagship/cheap model identifiers per provider. The ``openrouter`` refs
# are real OpenRouter catalog slugs (verified against api/v1/models) so the model
# the user picks resolves to the right price/context window from the same source;
# the dedicated-provider refs use each vendor's own current model ids. Note the
# OpenRouter slug uses a DOT (``claude-opus-4.8``) while Anthropic's own API uses
# a dash (``claude-opus-4-8``) — they are different namespaces.
# Fully-qualified J.A.R.N. model refs are ``<profile>/<model>``.
DEFAULT_MODELS = {
    "openrouter": {
        "main": "openrouter/anthropic/claude-opus-4.8",
        "subagent": "openrouter/anthropic/claude-haiku-4.5",
        "summarizer": "openrouter/anthropic/claude-haiku-4.5",
    },
    "anthropic": {
        "main": "anthropic/claude-opus-4-8",
        "subagent": "anthropic/claude-haiku-4-5",
        "summarizer": "anthropic/claude-haiku-4-5",
    },
    "openai": {
        "main": "openai/gpt-5.1",
        "subagent": "openai/gpt-5-mini",
        "summarizer": "openai/gpt-5-mini",
    },
    "google": {
        "main": "google/gemini-3.1-pro-preview",
        "subagent": "google/gemini-3.1-flash-lite",
        "summarizer": "google/gemini-3.1-flash-lite",
    },
    "mistral": {
        "main": "mistral/mistral-large-2512",
        "subagent": "mistral/mistral-small-2603",
        "summarizer": "mistral/mistral-small-2603",
    },
    "groq": {
        "main": "groq/llama-4-70b",
        "subagent": "groq/llama-4-8b",
        "summarizer": "groq/llama-4-8b",
    },
    "deepseek": {
        "main": "deepseek/deepseek-v4-pro",
        "subagent": "deepseek/deepseek-v4-flash",
        "summarizer": "deepseek/deepseek-v4-flash",
    },
    "together": {
        "main": "together/Qwen/Qwen3-Coder-480B",
        "subagent": "together/Qwen/Qwen3-Coder-30B",
        "summarizer": "together/Qwen/Qwen3-Coder-30B",
    },
    "fireworks": {
        "main": "fireworks/qwen3-coder-480b",
        "subagent": "fireworks/qwen3-coder-30b",
        "summarizer": "fireworks/qwen3-coder-30b",
    },
    "xai": {
        "main": "xai/grok-4.3",
        "subagent": "xai/grok-4.3",
        "summarizer": "xai/grok-4.3",
    },
    "ollama": {
        "main": "ollama/qwen3-coder:30b",
        "subagent": "ollama/qwen3-coder:7b",
        "summarizer": "ollama/qwen3-coder:7b",
    },
    "lmstudio": {
        "main": "lmstudio/qwen3-coder-30b",
        "subagent": "lmstudio/qwen3-coder-7b",
        "summarizer": "lmstudio/qwen3-coder-7b",
    },
    "openai_compatible": {
        "main": "openai_compatible/your-model",
        "subagent": "openai_compatible/your-model",
        "summarizer": "openai_compatible/your-model",
    },
}

#: Provider -> the env var the wizard suggests for that provider's API key.
PROVIDER_ENV_VARS = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "xai": "XAI_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}

#: Base URLs. Local providers need no key; cloud OpenAI-compatible ones need both.
PROVIDER_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "xai": "https://api.x.ai/v1",
    "ollama": "http://localhost:11434",
    "lmstudio": "http://localhost:1234/v1",
    "openai_compatible": "http://localhost:8000/v1",
}

#: Providers that require an API key (cloud + custom OpenAI-compatible endpoints).
#: ``openai_compatible`` is cloud because arbitrary endpoints usually need a key;
#: local servers (ollama, lmstudio) stay keyless.
CLOUD_PROVIDERS = (
    "openrouter", "anthropic", "openai", "google", "mistral",
    "groq", "deepseek", "together", "fireworks", "xai",
    "openai_compatible",
)

#: Profile that needs a user-supplied ``base_url`` during setup.
CUSTOM_OPENAI_PROFILE = "openai_compatible"

#: Providers whose ``base_url`` the setup wizard lets the user edit.
EDITABLE_BASE_URL_PROFILES = (
    CUSTOM_OPENAI_PROFILE,
    "ollama",
    "lmstudio",
)

#: All selectable providers, in wizard display order (custom before local).
ALL_PROVIDERS = (
    "openrouter", "anthropic", "openai", "google", "mistral",
    "groq", "deepseek", "together", "fireworks", "xai",
    "openai_compatible",
    "ollama", "lmstudio",
)

# Hard danger-guard patterns that always require explicit confirmation,
# even in YOLO mode. Authoritative copy lives in jarn.permissions.guard;
# duplicated here only for the generated config's documentation block.
DANGEROUS_COMMAND_HINTS = [
    "rm -rf",
    "git push --force",
    "git push -f",
    "dd if=",
    "mkfs",
    ":(){ :|:& };:",
]


def global_config_template(profile: str = "openrouter") -> str:
    """Return a commented YAML template for ``~/.jarn/config.yaml``."""
    env_var = PROVIDER_ENV_VARS.get(profile, "OPENROUTER_API_KEY")
    models = DEFAULT_MODELS.get(profile, DEFAULT_MODELS["openrouter"])
    return f"""\
# J.A.R.N. global configuration  (~/.jarn/config.yaml)
# Docs: https://github.com/chayapats/jarn/tree/main/docs

# Default provider profile and the model used for the main agent loop.
default_profile: {profile}
default_model: {models["main"]}

# Coarse trust level: plan | ask | auto-edit | yolo
permission_mode: ask

# Providers. API keys are *referenced*, never inlined:
#   ${{ENV_VAR}}                -> read from environment
#   keychain:jarn/<provider>    -> read from the OS keychain
providers:
  openrouter:
    type: openrouter
    api_key: ${{{env_var}}}
    base_url: {PROVIDER_BASE_URLS["openrouter"]}
  anthropic:
    type: anthropic
    api_key: ${{ANTHROPIC_API_KEY}}
  openai:
    type: openai
    api_key: ${{OPENAI_API_KEY}}
  ollama:
    type: ollama
    base_url: {PROVIDER_BASE_URLS["ollama"]}
  lmstudio:
    type: lmstudio
    base_url: {PROVIDER_BASE_URLS["lmstudio"]}

# Per-task model routing. Subagents and summarization can use cheaper models.
routing:
  main: {models["main"]}
  subagent: {models["subagent"]}
  summarizer: {models["summarizer"]}
  fallback: []          # ordered list of model refs tried on primary failure
  prompt_cache: auto    # auto | off — cache the prompt prefix where supported
  keep_alive: 1800      # secs to keep a local model + KV cache warm (Ollama/LM Studio)

# Session cost guardrails.
budget:
  per_session_usd: 5.0
  hard_stop: true       # stop the run when exceeded (false = warn only)
  warn_at_pct: 80

# Context-window management.
context:
  auto_compact: true
  compact_at_pct: 85
  # Repo map: compact ranked overview of the codebase injected into the agent.
  # repo_map: tool        # off | tool (on-demand via repo_map tool) | auto (also in system prompt)
  # repo_map_tokens: 1024  # token budget for map output (must be > 0)

# Plan-mode handoff: which mode `exit_plan_mode` escalates to once you approve a plan.
plan:
  exit_mode: auto-edit    # ask | auto-edit (the approval picker still offers the other)

# Execution backend (shell commands). Defaults to local; see docs/CONFIGURATION.md.
# OS-level sandbox is opt-in — enabling it changes execution behaviour.
# execution:
#   backend: local                 # local | sandbox
#   local_sandbox: off             # off | auto | require
#   sandbox_allow_network: true    # false = deny outbound network inside the sandbox
#   sandbox_writable: []           # extra paths the sandbox may write

# Persisted fine-grained permission rules (layered under permission_mode).
permissions:
  allow: []             # e.g. ["git status", "ls *", "npm test"]
  deny: []              # always blocked

observability:
  langsmith: false      # opt-in LangSmith tracing
  telemetry: false      # opt-in usage analytics (default OFF)
  log_level: info
  transcript: true      # append JSONL session transcripts to .jarn/sessions/

ui:
  theme: dark           # dark | light | high-contrast
  accent: cyan
  splash: compact       # full | compact (default) | off
  approval_diff_lines: 40   # max diff lines inline before "View full diff" in an approval

# ── Cross-vendor interop ──────────────────────────────────────────────────────
# Controls which context file is auto-loaded and whether ~/.claude / .claude/
# extension directories are scanned alongside ~/.jarn / .jarn/.
# compat:
#   context_files: ["JARN.md", "AGENTS.md", "CLAUDE.md"]  # first present wins
#   read_claude_dir: true   # set false to disable .claude/ skills/commands discovery

# ── Git safety (auto-checkpoint + /undo /redo) ────────────────────────────────
# When autocheckpoint is true, J.A.R.N. snapshots the git working tree before
# every agent turn so you can revert with /undo or re-apply with /redo.
# Snapshots live under refs/jarn/checkpoints/ — they NEVER move HEAD or the
# branch. Requires a git repo with at least one commit.
# git:
#   autocheckpoint: false   # set true to enable
#   checkpoint_mode: shadow # shadow (private refs only) | commit (reserved)

# ── Project knowledge base (wiki) ────────────────────────────────────────────
# A transparent, git-friendly per-project (and global) markdown knowledge base.
# Pages live under <project>/.jarn/wiki/pages/*.md and ~/.jarn/wiki/pages/*.md.
# When enabled the agent gets four tools: wiki_search, wiki_read, wiki_write,
# wiki_append. wiki_write/wiki_append require approval in ask mode; they are
# auto-allowed in auto-edit/yolo (same policy as file writes).
# The wiki index is injected into the system prompt at build time for trusted
# projects; global wiki is always available.
# wiki:
#   enabled: false
"""


def project_config_template() -> str:
    """Return a commented YAML template for ``<project>/.jarn/config.yaml``."""
    return """\
# J.A.R.N. project configuration  (.jarn/config.yaml)
# Committed with the repo. Overrides the global config for this project.

# Uncomment to pin a model for this project:
# default_model: openrouter/anthropic/claude-sonnet-4.5

# MCP servers exposing extra tools to the agent.
mcp_servers: []
#   - name: filesystem
#     transport: stdio
#     command: npx
#     args: ["-y", "@modelcontextprotocol/server-filesystem", "."]

# Lifecycle hooks. event: pre_tool | post_tool | post_edit | pre_commit |
#                          session_start | session_end
hooks: []
#   - event: post_edit
#     command: "ruff check --fix ."
#   - event: pre_commit
#     command: "pytest -q"
#     blocking: true

# Project-scoped permission rules.
permissions:
  allow: []
  deny: []
"""
