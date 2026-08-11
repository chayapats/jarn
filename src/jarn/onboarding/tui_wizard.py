"""Step-by-step onboarding wizard (Claude Code-style).

One question per screen, navigated with ↑/↓ + Enter; Esc goes back. The path
*branches* on answers — cloud providers ask for key storage; profiles in
:data:`EDITABLE_BASE_URL_PROFILES` collect a custom ``base_url``; local
providers skip the key step.

Falls back to the plain-text :func:`jarn.onboarding.wizard.run_wizard` when the
session is not a fully interactive TTY (pipes / CI).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from jarn.catalog import ModelCatalogSnapshot
from jarn.config import paths
from jarn.config.defaults import (
    ALL_PROVIDERS,
    CLOUD_PROVIDERS,
    CUSTOM_OPENAI_PROFILE,
    DEFAULT_MODELS,
    PROVIDER_BASE_URLS,
    PROVIDER_ENV_VARS,
)
from jarn.config.secrets import SecretResolutionError, file_fallback_notice, resolve
from jarn.onboarding.credentials import PendingCredentials
from jarn.onboarding.model_catalog import (
    load_setup_catalog,
    recommended_setup_model,
    selectable_setup_models,
    setup_catalog_status,
)
from jarn.onboarding.outcome import (
    SetupCommandError,
    SetupFailureKind,
    return_or_raise_setup_failure,
)
from jarn.onboarding.providers import provider_hint
from jarn.onboarding.state import SetupState, save_setup_state
from jarn.onboarding.wizard import (
    _advanced_fallback_refs,
    _advanced_model_ref,
    _build_config_dict,
    _detect_env_key,
    _recommended_provider,
    confirm_overwrite,
    normalize_base_url,
    profile_needs_base_url,
)
from jarn.providers import (
    qualify_model_ref,
    strip_profile,
    suggest_slug,
)
from jarn.tui.logo import TAGLINE
from jarn.tui.theme import ALL_THEMES, theme_name_for

_THEMES = [("dark", "Dark"), ("light", "Light"), ("high-contrast", "High contrast")]
_STORAGE = [
    ("env", "Read from an environment variable (recommended)"),
    ("keychain", "Paste it now → store in the OS keychain"),
]
_PROVIDER_HINTS = {p: provider_hint(p) for p in ALL_PROVIDERS}


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

    def __init__(
        self,
        *,
        chatgpt_ready: bool = False,
        local_providers: tuple[str, ...] = (),
        resume_state: SetupState | None = None,
        state_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.answers: dict[str, str] = dict(resume_state.answers) if resume_state else {}
        self.pending_credentials = PendingCredentials()
        self.history: list[str] = []
        self.step: str = resume_state.stage if resume_state else "provider"
        if self.answers.pop("_credential_pending", None):
            # Raw pasted keys are intentionally process-memory-only. A restarted
            # setup must collect it again rather than pretending the later step
            # is complete.
            self.step = "key"
        self.state_path = state_path
        self.result_path: Path | None = None
        self._base_url_error: str | None = None
        self._advanced_error: str | None = None
        self._cancelled = False
        # Model-step transient state: force the free-text box (after picking the
        # "enter manually" entry), the slug-hint shown on the last submit, and the
        # value that hint was about (resubmitting it unchanged accepts the slug).
        self._model_manual = False
        self._model_hint: str | None = None
        self._model_hinted_value: str | None = None
        self._model_catalog_snapshot: ModelCatalogSnapshot | None = None
        self._secret_notice: str | None = None
        # Probe the environment so we can offer an existing key and tag the
        # recommended provider (parity with the plain-text wizard).
        self.env_hit: tuple[str, str] | None = _detect_env_key()
        self.chatgpt_ready = chatgpt_ready
        self.local_providers = local_providers
        self.recommended: str = _recommended_provider(
            self.env_hit,
            chatgpt_ready=chatgpt_ready,
        )

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
        resumable = {
            "provider",
            "provider_detail",
            "storage",
            "key",
            "base_url",
            "model",
            "reasoning",
            "subagent_model",
            "summarizer_model",
            "fallback_models",
            "budget",
            "budget_warn",
            "budget_stop",
            "permissions",
            "theme",
            "confirm",
        }
        target = self.step if self.step in resumable else "provider"
        if target != "provider" and not self.answers.get("provider"):
            target = "provider"
        await self._goto(target, push=False)

    # -- navigation ---------------------------------------------------------

    async def _goto(self, step: str, *, push: bool = True) -> None:
        if push and self.step != step:
            self.history.append(self.step)
        if step == "model" and self.step != "model":
            # Fresh arrival at the model step (not an in-place re-render): start
            # from the pick-list, with no stale manual/hint state.
            self._model_manual = False
            self._model_hint = None
            self._model_hinted_value = None
        self.step = step
        save_setup_state(step, self.answers, path=self.state_path)
        await self._render_step()

    async def action_back(self) -> None:
        if self.history:
            self.step = self.history.pop()
            save_setup_state(self.step, self.answers, path=self.state_path)
            await self._render_step()
        else:
            previous = self._previous_step()
            if previous is None:
                save_setup_state(self.step, self.answers, path=self.state_path)
                self._cancelled = True
                self.exit()
            else:
                self.step = previous
                save_setup_state(self.step, self.answers, path=self.state_path)
                await self._render_step()

    def _previous_step(self) -> str | None:
        """Recover back navigation even after process restart (history is volatile)."""

        provider = self.answers.get("provider", "")
        group = self.answers.get("_provider_group", "")
        if self.step == "provider":
            return None
        if self.step == "provider_detail":
            return "provider"
        if self.step == "storage":
            return "provider_detail" if group in {"cloud", "advanced"} else "provider"
        if self.step == "key":
            return "storage"
        if self.step == "base_url":
            return "storage" if provider in CLOUD_PROVIDERS else "provider_detail"
        if self.step == "model":
            if profile_needs_base_url(provider):
                return "base_url"
            if provider in CLOUD_PROVIDERS:
                return "storage"
            return "provider_detail"
        if self.step == "reasoning":
            return "model"
        if self.step == "subagent_model":
            return "reasoning"
        if self.step == "summarizer_model":
            return "subagent_model"
        if self.step == "fallback_models":
            return "summarizer_model"
        if self.step == "budget":
            return "fallback_models"
        if self.step == "budget_warn":
            return "budget"
        if self.step == "budget_stop":
            return "budget_warn"
        if self.step == "permissions":
            return "budget_stop"
        if self.step == "theme":
            if group == "advanced":
                return "permissions"
            if provider == "codex_subscription":
                return "provider"
            if group == "advanced" or provider in {"ollama", "lmstudio"}:
                return "model"
            return "storage" if provider in CLOUD_PROVIDERS else "provider"
        if self.step == "confirm":
            return "theme"
        return "provider"

    def _env_key_present(self, provider: str) -> bool:
        """True when ``provider``'s env var currently holds a usable value."""
        import os

        env = PROVIDER_ENV_VARS.get(provider, f"{provider.upper()}_API_KEY")
        return bool(os.environ.get(env))

    def _key_ref_resolves(self, key_ref: str | None) -> bool:
        """True when ``key_ref`` resolves to a concrete value right now.

        A keychain ref points at a stored secret; a ``${ENV}`` ref needs the
        variable set. Anything that fails to resolve means the first turn would
        crash, so the wizard must collect a key before finishing.
        """
        if key_ref is None:
            return True
        try:
            return bool(resolve(key_ref))
        except SecretResolutionError:
            return False

    def _next_after_key(self) -> str:
        prov = self.answers.get("provider", "")
        if profile_needs_base_url(prov):
            return "base_url"
        if self.answers.get("_provider_group") != "advanced" and prov in CLOUD_PROVIDERS:
            return "theme"
        return "model"

    def _next_after_storage(self, storage: str) -> str:
        if storage == "keychain":
            return "key"
        # env storage: only continue when the ${ENV} reference actually
        # resolves; otherwise capture a key now so onboarding can't "succeed"
        # while leaving the first turn doomed to fail.
        if not self._key_ref_resolves(self.answers.get("key_ref")):
            return "key"
        return self._next_after_key()

    def _next_after_provider(self, provider: str) -> str:
        if provider == "codex_subscription":
            # Auth + the account-default model are verified after the Textual
            # stepper exits, before the staged config is committed.
            return "theme"
        if provider in CLOUD_PROVIDERS:
            return "storage"
        if profile_needs_base_url(provider):
            return "base_url"
        return "model"

    def _next_after_model(self) -> str:
        return "reasoning" if self.answers.get("_provider_group") == "advanced" else "theme"

    def _catalog_credential(self, prov: str) -> str | None:
        return self.pending_credentials.get(prov) or self.answers.get("key_ref")

    def _catalog_for_current_provider(self, *, refresh: bool = False) -> ModelCatalogSnapshot:
        prov = self.answers["provider"]
        if self._model_catalog_snapshot is None or refresh:
            self._model_catalog_snapshot = load_setup_catalog(
                prov,
                credential=self._catalog_credential(prov),
                base_url=self.answers.get("base_url") or PROVIDER_BASE_URLS.get(prov),
                include_hidden=self.answers.get("_provider_group") == "advanced",
            )
        return self._model_catalog_snapshot

    async def _catalog_with_visible_progress(
        self,
        *,
        refresh: bool = False,
    ) -> ModelCatalogSnapshot:
        if self._model_catalog_snapshot is not None and not refresh:
            return self._model_catalog_snapshot
        prov = self.answers["provider"]
        await self._set(
            f"Checking models reported by {prov}…",
            Static(
                "This is a read-only catalog request. "
                "It will stop at the configured catalog timeout."
            ),
        )
        return await asyncio.to_thread(
            self._catalog_for_current_provider,
            refresh=refresh,
        )

    def _remember_catalog_status(self, snapshot: ModelCatalogSnapshot, *, verified: bool) -> None:
        self.answers["_model_catalog_provenance"] = (
            snapshot.provenance_label if verified else setup_catalog_status(snapshot)
        )
        self.answers["_model_catalog_verified"] = "true" if verified else "false"

    def _apply_standard_catalog_model(self, snapshot: ModelCatalogSnapshot) -> None:
        """Select a live/default entry without treating bootstrap data as proof."""

        prov = self.answers["provider"]
        selected = recommended_setup_model(snapshot)
        if selected is not None:
            self.answers["model"] = selected.ref
            self._remember_catalog_status(snapshot, verified=True)
            return
        self.answers["model"] = DEFAULT_MODELS.get(prov, DEFAULT_MODELS["openrouter"])["main"]
        self.answers["_model_catalog_provenance"] = (
            f"{setup_catalog_status(snapshot)}; shipped bootstrap candidate, "
            "not proof of availability"
        )
        self.answers["_model_catalog_verified"] = "false"

    async def _set(self, title: str, widget) -> None:
        self.query_one("#title", Static).update(title)
        crumbs = "  ·  ".join(f"{k}: {v}" for k, v in self.answers.items() if not k.startswith("_"))
        self.query_one("#crumbs", Static).update(crumbs)
        body = self.query_one("#step", Vertical)
        await body.remove_children()
        if self._secret_notice:
            await body.mount(Static(f"[yellow]{self._secret_notice}[/yellow]"))
            self._secret_notice = None
        await body.mount(widget)
        widget.focus()

    def _option_list(
        self,
        items: list[tuple[str, str]],
        current: str | None = None,
        *,
        highlight: str | None = None,
    ) -> OptionList:
        opts = []
        highlight_idx: int | None = None
        for idx, (key, label) in enumerate(items):
            mark = "[cyan]●[/cyan] " if key == current else "  "
            opts.append(Option(f"{mark}{label}", id=f"opt:{key}"))
            if highlight is not None and key == highlight:
                highlight_idx = idx
        option_list = OptionList(*opts, id="step-list")
        if highlight_idx is not None:
            option_list.highlighted = highlight_idx
        return option_list

    # -- per-step rendering -------------------------------------------------

    async def _render_step(self) -> None:
        await getattr(self, f"_step_{self.step.replace('-', '_')}")()

    async def _step_provider(self) -> None:
        local_note = (
            f"  [green](found {', '.join(self.local_providers)})[/green]"
            if self.local_providers
            else ""
        )
        items = [
            (
                "codex_subscription",
                "Continue with ChatGPT  (subscription; no API key)"
                + (
                    "  [yellow]★ recommended — already signed in[/yellow]"
                    if self.chatgpt_ready
                    else ""
                ),
            ),
            (
                "anthropic",
                "Use Anthropic"
                + (
                    "  [yellow]★ recommended — key found[/yellow]"
                    if self.recommended == "anthropic"
                    else ""
                ),
            ),
            ("__cloud__", "Use another cloud provider"),
            ("__local__", f"Use a local model{local_note}"),
            ("__advanced__", "Advanced  (custom endpoints and full provider list)"),
        ]
        highlight = {
            "codex_subscription": "codex_subscription",
            "anthropic": "anthropic",
            "ollama": "__local__",
            "lmstudio": "__local__",
            CUSTOM_OPENAI_PROFILE: "__advanced__",
        }.get(self.recommended, "__cloud__")
        chosen = self.answers.get("provider")
        await self._set(
            "How do you want to connect?",
            self._option_list(items, chosen, highlight=chosen or highlight),
        )

    async def _step_provider_detail(self) -> None:
        group = self.answers.get("_provider_group")
        if group == "cloud":
            providers = [
                p for p in CLOUD_PROVIDERS if p not in ("anthropic", CUSTOM_OPENAI_PROFILE)
            ]
            title = "Choose another cloud provider"
            highlight = (
                self.env_hit[0]
                if self.env_hit is not None and self.env_hit[0] in providers
                else "openrouter"
            )
        elif group == "local":
            providers = [
                *self.local_providers,
                *(p for p in ("ollama", "lmstudio") if p not in self.local_providers),
            ]
            title = "Choose a local model server"
            highlight = providers[0]
        else:
            providers = [provider for provider in ALL_PROVIDERS if provider != "codex_subscription"]
            title = "Advanced provider selection"
            highlight = self.answers.get("provider") or self.recommended
        items = [(provider, f"{provider}  ({_PROVIDER_HINTS[provider]})") for provider in providers]
        await self._set(
            title,
            self._option_list(items, self.answers.get("provider"), highlight=highlight),
        )

    async def _step_storage(self) -> None:
        prov = self.answers["provider"]
        env = PROVIDER_ENV_VARS.get(prov, f"{prov.upper()}_API_KEY")
        # If the key is already in the environment, the env reference is the
        # recommended default; otherwise nudge the keychain (paste-now) path.
        if self._env_key_present(prov):
            env_label = f"Read from ${env} [green](found in your environment)[/green]"
        else:
            env_label = f"Read from ${env} — set it before launching"
        # OAuth exchanges persist their token immediately, so transactional
        # setup uses only environment references or process-memory paste here.
        # The standalone OpenRouter login remains available outside setup.
        items = [("env", env_label), _STORAGE[1]]
        await self._set(
            f"How should J.A.R.N. read your {prov} API key?",
            self._option_list(items, self.answers.get("storage")),
        )

    async def _step_key(self) -> None:
        prov = self.answers["provider"]
        env = PROVIDER_ENV_VARS.get(prov, f"{prov.upper()}_API_KEY")
        # When the env var is missing we land here to capture a key rather than
        # finishing with an unresolvable ${ENV} reference.
        if self.answers.get("storage") == "env":
            title = f"${env} is not set — paste your {prov} API key now (stored in the OS keychain)"
        else:
            title = "Paste your API key (stored in the OS keychain)"
        await self._set(title, Input(placeholder="sk-...", password=True, id="step-input"))

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

    def _default_model_id(self, prov: str) -> str:
        default_full = (
            self.answers.get("model")
            or DEFAULT_MODELS.get(prov, DEFAULT_MODELS["openrouter"])["main"]
        )
        default_id = strip_profile(default_full, prov)
        if prov == CUSTOM_OPENAI_PROFILE and default_id == "your-model":
            default_id = "gpt-4o"
        return default_id

    async def _step_model(self) -> None:
        prov = self.answers["provider"]
        default_id = self._default_model_id(prov)

        # Picked "enter manually" / came back to override a slug hint → free-text.
        if self._model_manual:
            await self._model_text_input(
                prov,
                default_id,
                catalog_notice="Manual entry; availability must pass the final readiness gate",
            )
            return

        # Every provider goes through the same catalog abstraction used by
        # /model, doctor, routing, and the pre-turn gate.  Static fallback rows
        # are not selectable because they do not prove endpoint/account access.
        snapshot = await self._catalog_with_visible_progress()
        discovered = selectable_setup_models(snapshot)
        if discovered:
            ids = [entry.model_id for entry in discovered]
            items = [(model_id, model_id) for model_id in ids]
            items.append(("__manual__", "Enter a model id manually…"))
            await self._set(
                f"Pick a model reported by {prov}  ({len(ids)} found; "
                f"{snapshot.provenance_label})",
                self._option_list(
                    items,
                    default_id if default_id in ids else None,
                    highlight=default_id if default_id in ids else None,
                ),
            )
            return

        endpoint = self.answers.get("base_url") or PROVIDER_BASE_URLS.get(prov, "")
        if profile_needs_base_url(prov):
            notice = (
                f"couldn't reach or verify the catalog at {endpoint} — "
                f"{setup_catalog_status(snapshot)}"
            )
        else:
            notice = setup_catalog_status(snapshot)
        await self._model_text_input(prov, default_id, catalog_notice=notice)

    async def _model_text_input(
        self,
        prov: str,
        default_id: str,
        *,
        catalog_notice: str | None = None,
    ) -> None:
        """Render manual entry with explicit unverified catalog provenance."""
        if self._model_hint:
            title = f"Model id for {prov} (manual, unverified) — {self._model_hint}"
        elif catalog_notice:
            title = f"{catalog_notice}  Enter a model id for {prov} manually (unverified)"
        elif prov == CUSTOM_OPENAI_PROFILE:
            title = "Model id on your endpoint  (e.g. gpt-4o, qwen3-coder)"
        else:
            title = f"Model id for {prov}  (e.g. deepseek/deepseek-v4-flash for OpenRouter)"
        await self._set(title, Input(value=default_id, id="step-input"))

    async def _step_reasoning(self) -> None:
        items = [
            ("default", "Provider/model default"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("xhigh", "Extra high"),
        ]
        await self._set(
            "Reasoning effort",
            self._option_list(items, self.answers.get("reasoning_effort", "default")),
        )

    async def _step_subagent_model(self) -> None:
        value = self.answers.get("routing_subagent") or self.answers.get("model", "")
        await self._set(
            "Subagent model  (use profile/model for another provider)",
            Input(value=value, id="step-input"),
        )

    async def _step_summarizer_model(self) -> None:
        value = (
            self.answers.get("routing_summarizer")
            or self.answers.get("routing_subagent")
            or self.answers.get("model", "")
        )
        await self._set(
            "Summarizer model  (use profile/model for another provider)",
            Input(value=value, id="step-input"),
        )

    async def _step_fallback_models(self) -> None:
        await self._set(
            "Fallback models, comma-separated  (blank for none)",
            Input(value=self.answers.get("routing_fallback", ""), id="step-input"),
        )

    async def _step_budget(self) -> None:
        error = f" — {self._advanced_error}" if self._advanced_error else ""
        await self._set(
            f"Maximum cost per session in USD{error}",
            Input(value=self.answers.get("budget_per_session_usd", "5.00"), id="step-input"),
        )

    async def _step_budget_warn(self) -> None:
        error = f" — {self._advanced_error}" if self._advanced_error else ""
        await self._set(
            f"Warn when this percentage of the budget is used{error}",
            Input(value=self.answers.get("budget_warn_at_pct", "80"), id="step-input"),
        )

    async def _step_budget_stop(self) -> None:
        items = [("true", "Stop automatically"), ("false", "Warn only")]
        await self._set(
            "When the session budget is reached",
            self._option_list(items, self.answers.get("budget_hard_stop", "true")),
        )

    async def _step_permissions(self) -> None:
        items = [
            ("plan", "Review only — read and plan"),
            ("ask", "Ask before changes — recommended"),
            ("auto-edit", "Edit workspace; ask before commands and external actions"),
            ("yolo", "Full access; hard safety blocks remain"),
        ]
        current = self.answers.get("permission_mode", "ask")
        await self._set(
            "Permission profile",
            self._option_list(items, current, highlight=current),
        )

    async def _step_theme(self) -> None:
        prov = self.answers.get("provider", "")
        if (
            self.answers.get("_provider_group") != "advanced"
            and prov in CLOUD_PROVIDERS
            and not self.answers.get("_model_catalog_provenance")
        ):
            snapshot = await self._catalog_with_visible_progress(refresh=True)
            self._apply_standard_catalog_model(snapshot)
        await self._set("Theme?", self._option_list(_THEMES, self.answers.get("theme", "dark")))

    async def _step_confirm(self) -> None:
        a = self.answers
        base_line = f"base_url: [b]{a['base_url']}[/b]\n" if a.get("base_url") else ""
        if a.get("_credential_pending"):
            key_ref = "(pasted; held in memory until verified commit)"
        else:
            key_ref = a.get(
                "key_ref",
                (
                    "(managed by Codex)"
                    if a.get("provider") == "codex_subscription"
                    else "(none — local)"
                ),
            )
        notice = ""
        if key_ref.startswith("file:"):
            from jarn.config.secrets import StoredSecret

            prov = a["provider"]
            env = PROVIDER_ENV_VARS.get(prov, f"{prov.upper()}_API_KEY")
            text = file_fallback_notice(
                StoredSecret(reference=key_ref, backend="file"),
                provider=prov,
                env_var=env,
            )
            if text:
                notice = f"\n[yellow]{text}[/yellow]\n"
        advanced = ""
        if a.get("_provider_group") == "advanced":
            from jarn.permissions.labels import permission_mode_name

            advanced = (
                f"reasoning: [b]{a.get('reasoning_effort', 'provider default')}[/b]\n"
                f"subagent: [b]{a.get('routing_subagent', a.get('model', ''))}[/b]\n"
                f"summary:  [b]{a.get('routing_summarizer', a.get('model', ''))}[/b]\n"
                f"fallback: [b]{a.get('routing_fallback') or '(none)'}[/b]\n"
                f"budget:   [b]${a.get('budget_per_session_usd', '5.00')}[/b] "
                f"(warn {a.get('budget_warn_at_pct', '80')}%, "
                f"hard stop {a.get('budget_hard_stop', 'true')})\n"
                "access:   [b]"
                f"{permission_mode_name(a.get('permission_mode', 'ask'))}[/b]\n"
            )
        catalog_line = ""
        if a.get("_model_catalog_provenance"):
            catalog_state = (
                "verified" if a.get("_model_catalog_verified") == "true" else "unverified"
            )
            catalog_line = (
                f"catalog:  [b]{catalog_state}[/b] — "
                f"{a['_model_catalog_provenance']}\n"
            )
        summary = (
            f"provider: [b]{'ChatGPT' if a['provider'] == 'codex_subscription' else a['provider']}[/b]\n"
            f"{base_line}"
            f"model:    [b]{a.get('model') or 'account default (checked before save)'}[/b]\n"
            f"{catalog_line}"
            f"key:      [b]{key_ref}[/b]\n"
            f"{advanced}"
            f"theme:    [b]{a.get('theme', 'dark')}[/b]\n"
            f"{notice}"
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
            if key in ("__cloud__", "__local__", "__advanced__"):
                self.answers["_provider_group"] = key.strip("_")
                await self._goto("provider_detail")
            else:
                await self._choose_provider(key)
        elif step == "provider_detail":
            await self._choose_provider(key)
        elif step == "storage":
            self.answers["storage"] = key
            if key == "env":
                self._set_env_key_ref()
            await self._goto(self._next_after_storage(key))
        elif step == "model":
            if key == "__manual__":
                # Drop the pick-list and render the free-text input instead.
                self._model_manual = True
                self._model_hint = None
                self._model_hinted_value = None
                await self._render_step()
            else:
                self._set_model(key)
                await self._goto(self._next_after_model())
        elif step == "reasoning":
            if key == "default":
                self.answers.pop("reasoning_effort", None)
            else:
                self.answers["reasoning_effort"] = key
            await self._goto("subagent_model")
        elif step == "budget_stop":
            self.answers["budget_hard_stop"] = key
            await self._goto("permissions")
        elif step == "permissions":
            self.answers["permission_mode"] = key
            await self._goto("theme")
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
                self.pending_credentials.set(prov, value)
                self.answers.pop("key_ref", None)
                self.answers["_credential_pending"] = "memory"
            else:
                self._set_env_key_ref()
            self._model_catalog_snapshot = None
            self.answers.pop("_model_catalog_provenance", None)
            self.answers.pop("_model_catalog_verified", None)
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
            if self.answers.get("_provider_group") != "advanced" and prov in CLOUD_PROVIDERS:
                self._model_catalog_snapshot = None
                self.answers.pop("_model_catalog_provenance", None)
                self.answers.pop("_model_catalog_verified", None)
                await self._goto("theme")
            else:
                self._model_catalog_snapshot = None
                await self._goto("model")
        elif self.step == "model":
            prov = self.answers["provider"]
            # Dot-vs-dash slug trap: if the typed slug looks like the wrong form,
            # surface the suggestion inline once. Resubmitting the same value
            # unchanged accepts it (the user's deliberate call).
            if value and value != self._model_hinted_value:
                hint = self._slug_hint(prov, value)
                if hint:
                    self._model_hint = hint
                    self._model_hinted_value = value
                    self._model_manual = True
                    await self._render_step()
                    return
            self._model_hint = None
            self._model_hinted_value = None
            self._set_model(value)
            await self._goto(self._next_after_model())
        elif self.step == "subagent_model":
            self.answers["routing_subagent"] = _advanced_model_ref(
                value or self.answers.get("model", ""), self.answers["provider"]
            )
            await self._goto("summarizer_model")
        elif self.step == "summarizer_model":
            self.answers["routing_summarizer"] = _advanced_model_ref(
                value or self.answers.get("routing_subagent") or self.answers.get("model", ""),
                self.answers["provider"],
            )
            await self._goto("fallback_models")
        elif self.step == "fallback_models":
            self.answers["routing_fallback"] = _advanced_fallback_refs(
                value, self.answers["provider"]
            )
            await self._goto("budget")
        elif self.step == "budget":
            try:
                parsed = float(value)
                if not 0 <= parsed < float("inf"):
                    raise ValueError
            except ValueError:
                self._advanced_error = "enter a finite number greater than or equal to 0"
                await self._render_step()
                return
            self._advanced_error = None
            self.answers["budget_per_session_usd"] = value
            await self._goto("budget_warn")
        elif self.step == "budget_warn":
            try:
                parsed_pct = int(value)
                if not 0 <= parsed_pct <= 100:
                    raise ValueError
            except ValueError:
                self._advanced_error = "enter a whole number from 0 through 100"
                await self._render_step()
                return
            self._advanced_error = None
            self.answers["budget_warn_at_pct"] = value
            await self._goto("budget_stop")

    def _set_model(self, value: str) -> None:
        prov = self.answers["provider"]
        model_ref = qualify_model_ref(value or self._default_model_id(prov), prov)
        self.answers["model"] = model_ref
        snapshot = self._model_catalog_snapshot
        verified = bool(
            snapshot
            and snapshot.availability_verified
            and any(
                entry.ref == model_ref
                and entry.account_available is not False
                and not entry.hidden
                and not entry.deprecated
                for entry in snapshot.models
            )
        )
        if snapshot is not None and verified:
            self._remember_catalog_status(snapshot, verified=True)
        else:
            self.answers["_model_catalog_provenance"] = (
                "Manual model entry; availability unverified until the final readiness gate"
            )
            self.answers["_model_catalog_verified"] = "false"

    def _slug_hint(self, prov: str, slug: str) -> str | None:
        """A dot-vs-dash ``suggest_slug`` hint for ``slug`` under ``prov`` (or None).

        Only meaningful for providers backed by a known :class:`ProviderType`;
        custom endpoints serve arbitrary ids so no hint is offered there.
        """
        from jarn.config.schema import ProviderType

        try:
            ptype = ProviderType(prov)
        except ValueError:
            return None
        return suggest_slug(ptype, qualify_model_ref(slug, prov).split("/", 1)[-1])

    def _set_env_key_ref(self) -> None:
        prov = self.answers["provider"]
        env = PROVIDER_ENV_VARS.get(prov, f"{prov.upper()}_API_KEY")
        self.pending_credentials.discard(prov)
        self.answers.pop("_credential_pending", None)
        self.answers["key_ref"] = f"${{{env}}}"
        self._model_catalog_snapshot = None
        self.answers.pop("_model_catalog_provenance", None)
        self.answers.pop("_model_catalog_verified", None)

    async def _choose_provider(self, provider: str) -> None:
        """Commit a concrete provider selected on either provider screen."""

        previous_provider = self.answers.get("provider")
        if self.step == "provider":
            self.answers["_provider_group"] = "standard"
        self.answers["provider"] = provider
        self.answers.pop("key_ref", None)
        self.answers.pop("_credential_pending", None)
        if previous_provider:
            self.pending_credentials.discard(previous_provider)
        self.pending_credentials.discard(provider)
        self.answers.pop("storage", None)
        self.answers.pop("base_url", None)
        self.answers.pop("model", None)
        self.answers.pop("_model_catalog_provenance", None)
        self.answers.pop("_model_catalog_verified", None)
        self._model_catalog_snapshot = None
        if self.env_hit is not None and self.env_hit[0] == provider:
            self.answers["storage"] = "env"
            self._set_env_key_ref()
            await self._goto(self._next_after_key())
        else:
            await self._goto(self._next_after_provider(provider))

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
            mode=a.get("permission_mode", "ask"),
            base_url_override=base_url,
            reasoning_effort=a.get("reasoning_effort"),
        )
        if a.get("_provider_group") == "advanced":
            config["routing"].update(
                {
                    "subagent": a.get("routing_subagent", model),
                    "summarizer": a.get("routing_summarizer", model),
                    "fallback": [item for item in a.get("routing_fallback", "").split(",") if item],
                }
            )
            config["budget"] = {
                "per_session_usd": float(a.get("budget_per_session_usd", "5")),
                "warn_at_pct": int(a.get("budget_warn_at_pct", "80")),
                "hard_stop": a.get("budget_hard_stop", "true") == "true",
            }
        # Every provider is staged only.  ``run_setup_tui`` owns the shared
        # verification/validation/transactional-commit gate after Textual exits.
        self.result_path = paths.global_config_path()
        self._saved_config = config
        self._saved_model = model
        self._saved_provider = provider
        save_setup_state("confirm", self.answers, path=self.state_path)
        self.exit()


def run_setup_tui(*, force: bool = False, propagate_errors: bool = False) -> Path | None:
    """Run the step wizard if interactive; else fall back to the text wizard."""
    import os
    import sys

    term = os.environ.get("TERM", "").strip().lower()
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or term in {"", "dumb", "unknown"}:
        from jarn.onboarding.wizard import run_wizard

        return run_wizard(force=force, propagate_errors=propagate_errors)

    from rich.console import Console as RichConsole
    from rich.prompt import Confirm

    from jarn.onboarding.flow import (
        SetupFlowError,
        finalize_setup,
        mark_setup_incomplete,
        set_setup_progress,
    )
    from jarn.onboarding.state import load_setup_state

    rc = RichConsole()
    try:
        proceed = confirm_overwrite(force=force)
    except (KeyboardInterrupt, EOFError):
        mark_setup_incomplete()
        rc.print("\n[yellow]Setup incomplete (cancelled).[/yellow] Resume with [b]jarn setup[/b].")
        return return_or_raise_setup_failure(
            SetupCommandError("The setup prompt was cancelled.", kind=SetupFailureKind.CANCELLED),
            propagate_errors=propagate_errors,
        )
    if not proceed:
        mark_setup_incomplete()
        return return_or_raise_setup_failure(
            SetupCommandError("The setup overwrite was declined.", kind=SetupFailureKind.CANCELLED),
            propagate_errors=propagate_errors,
        )

    try:
        set_setup_progress("in_progress")
    except SetupFlowError as exc:
        rc.print(f"[red]Setup incomplete (install state):[/red] {exc}")
        return return_or_raise_setup_failure(exc, propagate_errors=propagate_errors)

    from jarn.onboarding.wizard import _chatgpt_session_ready, _detect_local_providers

    saved = load_setup_state()
    resume = None
    try:
        if saved is not None and Confirm.ask(
            f"Resume setup from {saved.stage} (saved {saved.updated_at})?", default=True
        ):
            resume = saved
            rc.print(f"[green]✓[/green] Resuming at [b]{saved.stage}[/b].")
    except (KeyboardInterrupt, EOFError):
        mark_setup_incomplete()
        rc.print("\n[yellow]Setup incomplete (cancelled).[/yellow] Resume with [b]jarn setup[/b].")
        return return_or_raise_setup_failure(
            SetupCommandError("Setup resume was cancelled.", kind=SetupFailureKind.CANCELLED),
            propagate_errors=propagate_errors,
        )

    app = SetupApp(
        chatgpt_ready=_chatgpt_session_ready(),
        local_providers=_detect_local_providers(),
        resume_state=resume,
    )
    try:
        app.run()
    except (KeyboardInterrupt, EOFError, OSError) as exc:
        mark_setup_incomplete()
        rc.print(f"\n[red]Setup incomplete:[/red] {exc or 'terminal closed'}")
        rc.print("Your progress is saved. Resume with [b]jarn setup[/b].")
        kind = (
            SetupFailureKind.CANCELLED
            if isinstance(exc, (KeyboardInterrupt, EOFError))
            else SetupFailureKind.INTERNAL
        )
        return return_or_raise_setup_failure(
            SetupCommandError(str(exc) or "The terminal closed during setup.", kind=kind),
            propagate_errors=propagate_errors,
        )
    if app._cancelled:
        mark_setup_incomplete()
        rc.print("\n[yellow]Setup incomplete (cancelled).[/yellow] Resume with [b]jarn setup[/b].")
        return return_or_raise_setup_failure(
            SetupCommandError("Setup was cancelled by the user.", kind=SetupFailureKind.CANCELLED),
            propagate_errors=propagate_errors,
        )
    if app.result_path is None:
        mark_setup_incomplete()
        rc.print("\n[red]Setup incomplete:[/red] no configuration was confirmed.")
        return return_or_raise_setup_failure(
            SetupCommandError(
                "The setup UI exited without a confirmed configuration.",
                kind=SetupFailureKind.VERIFICATION,
            ),
            propagate_errors=propagate_errors,
        )

    try:
        return finalize_setup(
            app.answers,
            console=rc,
            pending_credentials=app.pending_credentials,
        )
    except (SetupFlowError, KeyboardInterrupt) as exc:
        mark_setup_incomplete()
        message = str(exc) if str(exc) else "setup cancelled"
        rc.print(f"\n[red]Setup incomplete at verification:[/red] {message}")
        rc.print("No configuration was changed. Retry with [b]jarn setup[/b].")
        failure = (
            exc
            if isinstance(exc, SetupCommandError)
            else SetupCommandError("Setup was interrupted.", kind=SetupFailureKind.CANCELLED)
        )
        return return_or_raise_setup_failure(failure, propagate_errors=propagate_errors)
