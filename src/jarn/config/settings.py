"""Easy, persistent settings editing — the backend for the ``/config`` command.

The full config is a two-tier YAML file (see :mod:`jarn.config.loader`). This
module exposes a **curated allowlist of safe scalar settings** that can be viewed
and changed without hand-editing YAML, and persists changes to the *global*
``~/.jarn/config.yaml`` with comments preserved (ruamel round-trip, atomic write
— same mechanism as :class:`jarn.permissions.rule_store.PermissionRuleStore`).

Structured / capability sections (``providers``, ``hooks``, ``mcp_servers``,
``async_subagents``, ``permissions``) are intentionally NOT settable here — they
need the wizard, the trust flow, or a deliberate file edit. ``/config set`` of an
unknown key tells the user so rather than silently corrupting the file.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


@dataclass(frozen=True, slots=True)
class Setting:
    """One settable key: its dotted path, value type, optional choices, group."""

    key: str
    type: str          # "str" | "int" | "float" | "bool" | "enum"
    group: str
    choices: tuple[str, ...] = ()


#: The curated, safe, scalar settings exposed to ``/config``. Dotted keys map
#: 1:1 onto both the :class:`~jarn.config.schema.Config` attribute path and the
#: YAML path. Ordered for a readable grouped display.
SETTINGS: tuple[Setting, ...] = (
    Setting("permission_mode", "enum", "general", ("plan", "ask", "auto-edit", "yolo")),
    Setting("default_model", "str", "models"),
    Setting("routing.main", "str", "models"),
    Setting("routing.subagent", "str", "models"),
    Setting("routing.summarizer", "str", "models"),
    Setting("policy.profile", "enum", "policy",
            ("", "trusted-repo", "review-only", "sandbox-required", "ci", "offline")),
    Setting("policy.web_tools", "bool", "policy"),
    Setting("execution.backend", "enum", "execution", ("local", "sandbox", "docker")),
    Setting("execution.local_sandbox", "enum", "execution", ("off", "auto", "require")),
    Setting("execution.sandbox_allow_network", "bool", "execution"),
    Setting("execution.docker_image", "str", "execution"),
    Setting("budget.per_session_usd", "float", "budget"),
    Setting("budget.hard_stop", "bool", "budget"),
    Setting("budget.warn_at_pct", "int", "budget"),
    Setting("context.auto_compact", "bool", "context"),
    Setting("context.compact_at_pct", "int", "context"),
    Setting("context.repo_map", "enum", "context", ("off", "tool", "auto")),
    Setting("context.repo_map_tokens", "int", "context"),
    Setting("wiki.enabled", "bool", "features"),
    Setting("git.autocheckpoint", "bool", "features"),
    Setting("observability.transcript", "bool", "features"),
    Setting("ui.theme", "enum", "ui", ("dark", "light", "high-contrast")),
    Setting("ui.accent", "str", "ui"),
)

_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


class SettingError(ValueError):
    """Raised when a key is not settable or a value can't be coerced."""


def is_settable(key: str) -> bool:
    return key in _BY_KEY


def setting_for(key: str) -> Setting | None:
    return _BY_KEY.get(key)


def get_value(config: object, key: str) -> object:
    """Read the current value of ``key`` (dotted) off the live ``config``.

    ``permission_mode`` is returned as its ``.value`` string so it round-trips
    cleanly to YAML; everything else is returned as-is (``None`` for unset).
    """
    obj: object = config
    for part in key.split("."):
        obj = getattr(obj, part)
    if hasattr(obj, "value"):   # PermissionMode (str enum)
        return obj.value
    return obj


def coerce(key: str, raw: str) -> object:
    """Coerce the string ``raw`` to the type declared for ``key``.

    Raises :class:`SettingError` on an unknown key, a bad enum choice, or a
    value that doesn't parse as the declared type.
    """
    spec = _BY_KEY.get(key)
    if spec is None:
        raise SettingError(
            f"{key!r} is not a settable key. Run /config to see settable keys; "
            "edit ~/.jarn/config.yaml for advanced/structured settings."
        )
    if spec.type == "enum":
        if raw not in spec.choices:
            raise SettingError(
                f"{key} must be one of {', '.join(repr(c) for c in spec.choices)} (got {raw!r})."
            )
        return raw
    if spec.type == "bool":
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise SettingError(f"{key} must be a boolean (true/false), got {raw!r}.")
    if spec.type == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise SettingError(f"{key} must be an integer, got {raw!r}.") from exc
    if spec.type == "float":
        try:
            return float(raw)
        except ValueError as exc:
            raise SettingError(f"{key} must be a number, got {raw!r}.") from exc
    return raw  # str


def _yaml() -> YAML:
    y = YAML()  # round-trip mode: preserves comments + style
    y.preserve_quotes = True
    return y


class ConfigStore:
    """Read/round-trip-write a single config.yaml (comment-preserving, atomic)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read_text(self) -> str | None:
        """Current raw file text, or None when the file does not exist."""
        if not self.path.is_file():
            return None
        return self.path.read_text(encoding="utf-8")

    def restore(self, text: str | None) -> None:
        """Restore the file to ``text`` (or remove it if ``text`` is None)."""
        if text is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(text, encoding="utf-8")

    def set(self, key: str, value: object) -> None:
        """Set ``key`` (dotted) to ``value`` in the file, preserving comments."""
        data = self._load()
        parts = key.split(".")
        node = data
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
        self._atomic_write(data)

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            loaded = _yaml().load(self.path.read_text(encoding="utf-8"))
        except (OSError, YAMLError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _atomic_write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        _yaml().dump(data, buf)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        os.replace(tmp, self.path)


class ConfigPanel:
    """State model for the interactive ``/config`` settings panel (arrow-key).

    Framework-agnostic (no prompt_toolkit) so it is fully unit-testable: the REPL
    view drives it with key events and renders :meth:`render_lines`.

    - ``get_config()`` returns the live config (read fresh — it is replaced on
      each successful save).
    - ``apply(key, raw)`` persists ``key`` = ``raw`` and returns ``(ok, message)``.

    Navigation: ↑/↓ moves the selection. Enter on a **bool** toggles it, on an
    **enum** cycles to the next choice, on a **str/int/float** enters edit mode
    (type a value, Enter saves, Esc cancels). Each toggle/cycle/save persists
    immediately via ``apply``.
    """

    NAVIGATE = "navigate"
    EDIT = "edit"

    def __init__(self, get_config, apply) -> None:  # noqa: ANN001
        self.settings = list(SETTINGS)
        self._get_config = get_config
        self._apply = apply
        self.index = 0
        self.mode = self.NAVIGATE
        self.buffer = ""
        self.message = ""

    @property
    def editing(self) -> bool:
        return self.mode == self.EDIT

    def current(self) -> Setting:
        return self.settings[self.index]

    def value_of(self, spec: Setting) -> object:
        return get_value(self._get_config(), spec.key)

    def move(self, delta: int) -> None:
        if self.editing:
            return
        self.index = (self.index + delta) % len(self.settings)

    def activate(self) -> None:
        """Enter on the selected row: toggle / cycle / begin editing."""
        if self.editing:
            return
        spec = self.current()
        if spec.type == "bool":
            self._commit(spec, "false" if bool(self.value_of(spec)) else "true")
        elif spec.type == "enum":
            choices = list(spec.choices)
            cur = self.value_of(spec)
            cur = "" if cur is None else str(cur)
            i = choices.index(cur) if cur in choices else -1
            self._commit(spec, choices[(i + 1) % len(choices)])
        else:
            self.mode = self.EDIT
            cur = self.value_of(spec)
            self.buffer = "" if cur is None else str(cur)
            self.message = "editing — type a value · Enter save · Esc cancel"

    def type_text(self, text: str) -> None:
        if self.editing:
            self.buffer += text

    def backspace(self) -> None:
        if self.editing:
            self.buffer = self.buffer[:-1]

    def cancel_edit(self) -> None:
        if self.editing:
            self.mode = self.NAVIGATE
            self.buffer = ""
            self.message = "cancelled"

    def commit_edit(self) -> None:
        if not self.editing:
            return
        spec, raw = self.current(), self.buffer
        self.mode = self.NAVIGATE
        self.buffer = ""
        self._commit(spec, raw)

    def _commit(self, spec: Setting, raw: str) -> None:
        _ok, msg = self._apply(spec.key, raw)
        self.message = msg

    def render_lines(self) -> list[tuple[str, str]]:
        """(style, text) fragments for a prompt_toolkit FormattedTextControl."""
        out: list[tuple[str, str]] = [
            ("bold", " Settings  "),
            ("#7c8f94", "↑/↓ move · Enter toggle/edit · Esc close\n"),
        ]
        last_group = ""
        for i, spec in enumerate(self.settings):
            if spec.group != last_group:
                out.append(("bold #7c8f94", f"\n {spec.group}\n"))
                last_group = spec.group
            selected = i == self.index
            if self.editing and selected:
                shown = self.buffer + "▏"   # ▏ caret
            else:
                val = self.value_of(spec)
                shown = "(unset)" if val is None or val == "" else str(val)
            marker = "›" if selected else " "  # ›
            out.append(
                ("reverse" if selected else "", f" {marker} {spec.key}: {shown}\n")
            )
        if self.message:
            out.append(("#7c8f94", f"\n {self.message}\n"))
        return out


def format_settings(config: object) -> str:
    """Render the current settable settings, grouped, as Rich-markup text."""
    lines = ["[b]Settings[/b] [dim](editable with /config set <key> <value>)[/dim]"]
    last_group = ""
    for spec in SETTINGS:
        if spec.group != last_group:
            lines.append(f"\n[dim]── {spec.group} ──[/dim]")
            last_group = spec.group
        val = get_value(config, spec.key)
        shown = "[dim](unset)[/dim]" if val is None or val == "" else str(val)
        lines.append(f"  {spec.key} = {shown}")
    lines.append(
        "\n[dim]Changes persist to ~/.jarn/config.yaml. Structured keys "
        "(providers/hooks/mcp_servers) — edit the file or run `jarn setup`.[/dim]"
    )
    return "\n".join(lines)
