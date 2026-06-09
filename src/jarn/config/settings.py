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
