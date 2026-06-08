"""Step-by-step onboarding wizard (Claude Code-style).

One question per screen, navigated with ↑/↓ + Enter; Esc goes back. The path
*branches* on answers — cloud providers ask for key storage; profiles in
:data:`EDITABLE_BASE_URL_PROFILES` collect a custom ``base_url``; local
providers skip the key step.

Falls back to the plain-text :func:`jarn.onboarding.wizard.run_wizard` when the
session is not a fully interactive TTY (pipes / CI).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from jarn.config import paths
from jarn.config.defaults import (
    ALL_PROVIDERS,
    CLOUD_PROVIDERS,
    CUSTOM_OPENAI_PROFILE,
    DEFAULT_MODELS,
    PROVIDER_BASE_URLS,
    PROVIDER_ENV_VARS,
)
from jarn.config.secrets import store_keychain
from jarn.onboarding.wizard import (
    _CONFIG_HEADER,
    _build_config_dict,
    confirm_overwrite,
    normalize_base_url,
    profile_needs_base_url,
    validate_config,
)
from jarn.tui.logo import TAGLINE
from jarn.tui.theme import ALL_THEMES, theme_name_for

_THEMES = [("dark", "Dark"), ("light", "Light"), ("high-contrast", "High contrast")]
_STORAGE = [
    ("env", "Read from an environment variable (recommended)"),
    ("keychain", "Paste it now → store in the OS keychain"),
]


def _provider_hint(name: str) -> str:
    if name == CUSTOM_OPENAI_PROFILE:
        return "custom"
    if name in CLOUD_PROVIDERS:
        return "cloud"
    return "local"


_PROVIDER_HINTS = {p: _provider_hint(p) for p in ALL_PROVIDERS}


class SetupApp(App):
    """A small step machine. Each step renders one prompt into ``#step``."""

    CSS = """
    Screen { align: center middle; }
    #card { width: 78; height: auto; max-height: 90%; padding: 1 2; border: thick $primary; background: $surface; }
    #brand { color: $accent; }
    #crumbs { color: $text-muted; margin-bottom: 1; }
    #title { text-style: bold; margin-bottom: 1; }
    #step { height: auto; }
    OptionList { height: auto; max-height: 14; border: none; }
    Input { margin-top: 1; }
    #help { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [("escape", "back", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[str, str] = {}
        self.history: list[str] = []
        self.step: str = "provider"
        self.result_path: Path | None = None
        self._base_url_error: str | None = None
        self._cancelled = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="card"):
            yield Static(f"[b]{TAGLINE}[/b]", id="brand")
            yield Static("", id="crumbs")
            yield Static("", id="title")
            yield Vertical(id="step")
            yield Static("↑/↓ select · Enter confirm · Esc back", id="help")

    async def on_mount(self) -> None:
        for theme in ALL_THEMES.values():
            self.register_theme(theme)
        self.theme = theme_name_for(self.answers.get("theme", "dark"))
        await self._goto("provider", push=False)

    # -- navigation ---------------------------------------------------------

    async def _goto(self, step: str, *, push: bool = True) -> None:
        if push and self.step != step:
            self.history.append(self.step)
        self.step = step
        await self._render_step()

    async def action_back(self) -> None:
        if self.history:
            self.step = self.history.pop()
            await self._render_step()
        else:
            self._cancelled = True
            self.exit()

    def _next_after_key(self) -> str:
        prov = self.answers.get("provider", "")
        if profile_needs_base_url(prov):
            return "base_url"
        return "model"

    def _next_after_storage(self, storage: str) -> str:
        if storage == "keychain":
            return "key"
        return self._next_after_key()

    def _next_after_provider(self, provider: str) -> str:
        if provider in CLOUD_PROVIDERS:
            return "storage"
        if profile_needs_base_url(provider):
            return "base_url"
        return "model"

    async def _set(self, title: str, widget) -> None:
        self.query_one("#title", Static).update(title)
        crumbs = "  ·  ".join(f"{k}: {v}" for k, v in self.answers.items())
        self.query_one("#crumbs", Static).update(crumbs)
        body = self.query_one("#step", Vertical)
        await body.remove_children()
        await body.mount(widget)
        widget.focus()

    def _option_list(self, items: list[tuple[str, str]], current: str | None = None) -> OptionList:
        opts = []
        for key, label in items:
            mark = "[cyan]●[/cyan] " if key == current else "  "
            opts.append(Option(f"{mark}{label}", id=f"opt:{key}"))
        return OptionList(*opts, id="step-list")

    # -- per-step rendering -------------------------------------------------

    async def _render_step(self) -> None:
        await getattr(self, f"_step_{self.step.replace('-', '_')}")()

    async def _step_provider(self) -> None:
        items = [(p, f"{p}  ({_PROVIDER_HINTS[p]})") for p in ALL_PROVIDERS]
        await self._set("Which model provider?",
                        self._option_list(items, self.answers.get("provider")))

    async def _step_storage(self) -> None:
        prov = self.answers["provider"]
        env = PROVIDER_ENV_VARS.get(prov, f"{prov.upper()}_API_KEY")
        items = [("env", f"Read from ${env} (recommended)"), _STORAGE[1]]
        await self._set(f"How should J.A.R.N. read your {prov} API key?",
                        self._option_list(items, self.answers.get("storage")))

    async def _step_key(self) -> None:
        await self._set("Paste your API key (stored in the OS keychain)",
                        Input(placeholder="sk-...", password=True, id="step-input"))

    async def _step_base_url(self) -> None:
        prov = self.answers["provider"]
        default = self.answers.get("base_url") or PROVIDER_BASE_URLS.get(
            prov, "http://localhost:8000/v1"
        )
        err = f" — {self._base_url_error}" if self._base_url_error else ""
        if prov == "ollama":
            hint = "Ollama host URL (no /v1 suffix)"
        elif prov == CUSTOM_OPENAI_PROFILE:
            hint = "bare host → /v1 appended"
        else:
            hint = "include /v1 when required"
        await self._set(
            f"API base URL for {prov}{err}  ({hint})",
            Input(value=default, placeholder="http://localhost:11434", id="step-input"),
        )

    async def _step_model(self) -> None:
        from jarn.providers import strip_profile

        prov = self.answers["provider"]
        default_full = self.answers.get("model") or DEFAULT_MODELS.get(
            prov, DEFAULT_MODELS["openrouter"]
        )["main"]
        default_id = strip_profile(default_full, prov)
        if prov == CUSTOM_OPENAI_PROFILE and default_id == "your-model":
            default_id = "gpt-4o"
        if prov == CUSTOM_OPENAI_PROFILE:
            title = "Model id on your endpoint  (e.g. gpt-4o, qwen3-coder)"
        else:
            title = (
                f"Model id for {prov}  (e.g. deepseek/deepseek-v4-flash for OpenRouter)"
            )
        await self._set(title, Input(value=default_id, id="step-input"))

    async def _step_theme(self) -> None:
        await self._set("Theme?", self._option_list(_THEMES, self.answers.get("theme", "dark")))

    async def _step_confirm(self) -> None:
        a = self.answers
        base_line = f"base_url: [b]{a['base_url']}[/b]\n" if a.get("base_url") else ""
        summary = (
            f"provider: [b]{a['provider']}[/b]\n"
            f"{base_line}"
            f"model:    [b]{a.get('model','')}[/b]\n"
            f"key:      [b]{a.get('key_ref','(none — local)')}[/b]\n"
            f"theme:    [b]{a.get('theme','dark')}[/b]\n"
        )
        body = Vertical(
            Static(summary),
            self._option_list([("save", "Save configuration"), ("back", "Go back")], None),
        )
        self.query_one("#title", Static).update("Ready?")
        self.query_one("#crumbs", Static).update("")
        container = self.query_one("#step", Vertical)
        await container.remove_children()
        await container.mount(body)
        body.query_one(OptionList).focus()

    # -- answer handlers ----------------------------------------------------

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        key = (event.option.id or "").removeprefix("opt:")
        step = self.step
        if step == "provider":
            self.answers["provider"] = key
            self.answers.pop("key_ref", None)
            self.answers.pop("base_url", None)
            await self._goto(self._next_after_provider(key))
        elif step == "storage":
            self.answers["storage"] = key
            if key == "env":
                self._set_env_key_ref()
            await self._goto(self._next_after_storage(key))
        elif step == "theme":
            self.answers["theme"] = key
            self.theme = theme_name_for(key)
            await self._goto("confirm")
        elif step == "confirm":
            if key == "save":
                self._finish()
            else:
                await self.action_back()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if self.step == "key":
            prov = self.answers["provider"]
            if value:
                store_keychain("jarn", prov, value)
                self.answers["key_ref"] = f"keychain:jarn/{prov}"
            else:
                self._set_env_key_ref()
            await self._goto(self._next_after_key())
        elif self.step == "base_url":
            prov = self.answers["provider"]
            try:
                self.answers["base_url"] = normalize_base_url(prov, value)
            except ValueError as exc:
                self._base_url_error = str(exc)
                await self._render_step()
                return
            self._base_url_error = None
            await self._goto("model")
        elif self.step == "model":
            from jarn.providers import qualify_model_ref, strip_profile

            prov = self.answers["provider"]
            default_id = strip_profile(
                DEFAULT_MODELS.get(prov, DEFAULT_MODELS["openrouter"])["main"], prov
            )
            if prov == CUSTOM_OPENAI_PROFILE and default_id == "your-model":
                default_id = "gpt-4o"
            self.answers["model"] = qualify_model_ref(value or default_id, prov)
            await self._goto("theme")

    def _set_env_key_ref(self) -> None:
        prov = self.answers["provider"]
        env = PROVIDER_ENV_VARS.get(prov, f"{prov.upper()}_API_KEY")
        self.answers["key_ref"] = f"${{{env}}}"

    def _finish(self) -> None:
        a = self.answers
        provider = a["provider"]
        model = a.get("model") or DEFAULT_MODELS.get(provider, DEFAULT_MODELS["openrouter"])["main"]
        key_ref = a.get("key_ref") if provider in CLOUD_PROVIDERS else None
        base_url = a.get("base_url") if profile_needs_base_url(provider) else None
        config = _build_config_dict(
            provider,
            key_ref,
            model,
            a.get("theme", "dark"),
            base_url_override=base_url,
        )
        path = paths.global_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _CONFIG_HEADER + yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        self.result_path = path
        self._saved_config = config
        self._saved_model = model
        self._saved_provider = provider
        self.exit()


def run_setup_tui(*, force: bool = False) -> Path | None:
    """Run the step wizard if interactive; else fall back to the text wizard."""
    import sys

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        from jarn.onboarding.wizard import run_wizard

        return run_wizard(force=force)

    if not confirm_overwrite(force=force):
        return paths.global_config_path()

    app = SetupApp()
    app.run()
    if app._cancelled:
        return None
    if app.result_path is None:
        return None

    config = getattr(app, "_saved_config", None)
    if config is not None and app._saved_provider in CLOUD_PROVIDERS:
        validate_config(app._saved_provider, app._saved_model, config)

    return app.result_path
