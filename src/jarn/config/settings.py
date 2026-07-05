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

from dataclasses import dataclass
from pathlib import Path

from jarn.config.yaml_store import (
    ConfigCorruptError,  # noqa: F401 - re-exported for callers (controller/tests)
    atomic_write_yaml,
    load_yaml_doc,
)


@dataclass(frozen=True, slots=True)
class Setting:
    """One settable setting: dotted key, type, friendly category/label/help."""

    key: str
    type: str          # "str" | "int" | "float" | "bool" | "enum"
    group: str         # friendly category (also the tab label)
    label: str         # human title shown in the panel
    desc: str          # one-line plain-language help
    choices: tuple[str, ...] = ()


def _s(key, type, group, label, desc, choices=()):  # noqa: A002 - terse builder
    return Setting(key, type, group, label, desc, tuple(choices))


#: The curated, safe, scalar settings exposed to ``/config``. Grouped into a few
#: plain-language categories with human labels + one-line help so the panel reads
#: like a real settings screen, not a YAML dump. Dotted ``key`` maps 1:1 onto the
#: Config attribute path and the YAML path.
SETTINGS: tuple[Setting, ...] = (
    # ── Models ──
    _s("default_model", "str", "Models", "Model",
       "The model the agent uses by default."),
    _s("routing.main", "str", "Models", "Main model",
       "Override the model for the main agent (blank = use Model above)."),
    _s("routing.subagent", "str", "Models", "Subagent model",
       "A cheaper model for delegated subagent tasks (blank = same)."),
    _s("routing.summarizer", "str", "Models", "Summarizer model",
       "Model used to compact long conversations (blank = same)."),
    # ── Safety ──
    _s("permission_mode", "enum", "Safety", "Permission mode",
       "How much to confirm before edits / shell / network.",
       ("plan", "ask", "auto-edit", "yolo")),
    _s("policy.web_tools", "bool", "Safety", "Web tools",
       "Let the agent use web search & fetch."),
    # ── Sandbox ──
    _s("execution.backend", "enum", "Sandbox", "Run commands in",
       "Where tools run: your host, a Docker container, or a remote sandbox.",
       ("local", "docker", "sandbox")),
    _s("execution.local_sandbox", "enum", "Sandbox", "OS sandbox",
       "Kernel-level isolation for host commands (sandbox-exec / bwrap).",
       ("off", "auto", "require")),
    _s("execution.sandbox_allow_network", "bool", "Sandbox", "Sandbox network",
       "Allow network access from inside the sandbox."),
    _s("execution.docker_image", "str", "Sandbox", "Docker image",
       "Container image used when running in Docker."),
    # ── Budget ──
    _s("budget.per_session_usd", "float", "Budget", "Session budget ($)",
       "Warn or stop after this much spend per session (0 = no limit)."),
    _s("budget.hard_stop", "bool", "Budget", "Hard stop",
       "Stop the session when the budget is exceeded (vs just warn)."),
    _s("budget.warn_at_pct", "int", "Budget", "Warn at (%)",
       "Warn when spend reaches this percent of the budget."),
    # ── Behavior ──
    _s("context.auto_compact", "bool", "Behavior", "Auto-compact",
       "Summarize the conversation automatically as context fills up."),
    _s("context.compact_at_pct", "int", "Behavior", "Compact at (%)",
       "Context fullness that triggers auto-compact."),
    _s("context.repo_map", "enum", "Behavior", "Repo map",
       "Give the agent a map of your codebase (tool / auto-inject / off).",
       ("off", "tool", "auto")),
    _s("context.repo_map_tokens", "int", "Behavior", "Repo map size",
       "Token budget for the repo map."),
    _s("wiki.enabled", "bool", "Behavior", "Wiki",
       "Enable the agent's markdown knowledge base (/wiki)."),
    _s("git.autocheckpoint", "bool", "Behavior", "Auto-checkpoint",
       "Snapshot files before each turn so /undo can revert."),
    _s("observability.transcript", "bool", "Behavior", "Session transcript",
       "Write a JSONL log of each session under .jarn/sessions."),
    # ── Appearance ──
    _s("ui.theme", "enum", "Appearance", "Theme",
       "Color theme.", ("dark", "light", "high-contrast")),
    _s("ui.accent", "str", "Appearance", "Accent color",
       "Brand accent color (e.g. cyan, magenta)."),
    _s("ui.splash", "enum", "Appearance", "Splash",
       "Startup banner: full / compact / off (first run always shows full once).",
       ("full", "compact", "off")),
    _s("ui.approval_diff_lines", "int", "Appearance", "Approval diff lines",
       "Max diff lines shown inline before a write approval offers 'View full diff'."),
    _s("ui.notify", "enum", "Appearance", "Notifications",
       "How to alert you when a long turn finishes or an approval is needed.",
       ("off", "bell", "desktop", "both")),
    _s("ui.notify_min_secs", "int", "Appearance", "Notify after (secs)",
       "Minimum turn length before a turn-end notification fires (0 = always)."),
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


class ConfigStore:
    """Read/round-trip-write a single config.yaml (comment-preserving, atomic).

    A corrupt or unreadable file is never silently wiped: :meth:`set` raises
    :class:`ConfigCorruptError` (after saving a ``.bak``) instead of overwriting
    the file with a near-empty dict. A missing file bootstraps from ``{}``.
    """

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
        """Set ``key`` (dotted) to ``value`` in the file, preserving comments.

        Raises :class:`ConfigCorruptError` if the file is unreadable; the file is
        left untouched and a ``.bak`` is saved.
        """
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
        return load_yaml_doc(self.path)

    def _atomic_write(self, data: dict) -> None:
        atomic_write_yaml(self.path, data)


# Style tokens (theme-agnostic where it matters; cyan accent matches the brand).
_C_ACCENT = "#22d3ee"
_C_DIM = "#7c8f94"
_C_ON = "#3fb950"
_C_WARN = "#d29922"
_C_ERR = "#f85149"


class ConfigPanel:
    """State model for the interactive ``/config`` settings panel.

    Framework-agnostic (no prompt_toolkit) so it is fully unit-testable: the REPL
    view drives it with key events and renders :meth:`render_lines`.

    Two-dimensional, Claude-Code-style navigation:
    - **categories** run horizontally as tabs (``←``/``→``) — General, Models,
      Policy, Execution, Budget, Context, Features, UI;
    - **settings** in the active category run vertically (``↑``/``↓``).

    Enter on the selected setting toggles a **bool**, cycles an **enum**, or edits
    a **str/int/float** in place (type · Enter saves · Esc cancels). Every change
    persists immediately via ``apply(key, raw) -> (ok, message)``. ``get_config()``
    returns the live config (read fresh — replaced on each successful save).
    """

    NAVIGATE = "navigate"
    EDIT = "edit"

    def __init__(self, get_config, apply) -> None:  # noqa: ANN001
        self.settings = list(SETTINGS)
        self._get_config = get_config
        self._apply = apply
        # ordered, de-duplicated category list (tab order)
        self.groups: list[str] = []
        for s in self.settings:
            if s.group not in self.groups:
                self.groups.append(s.group)
        self.cat_index = 0
        self.item_index = 0
        self.mode = self.NAVIGATE
        self.buffer = ""
        self.message = ""
        self.message_ok = True

    # -- selection ----------------------------------------------------------

    @property
    def editing(self) -> bool:
        return self.mode == self.EDIT

    @property
    def category(self) -> str:
        return self.groups[self.cat_index]

    def items(self) -> list[Setting]:
        """Settings in the active category."""
        return [s for s in self.settings if s.group == self.category]

    def current(self) -> Setting:
        return self.items()[self.item_index]

    def value_of(self, spec: Setting) -> object:
        return get_value(self._get_config(), spec.key)

    def select_key(self, key: str) -> None:
        """Jump selection to the setting named ``key`` (category + item)."""
        spec = next(s for s in self.settings if s.key == key)
        self.cat_index = self.groups.index(spec.group)
        self.item_index = self.items().index(spec)

    def move_category(self, delta: int) -> None:
        if self.editing:
            return
        self.cat_index = (self.cat_index + delta) % len(self.groups)
        self.item_index = 0
        self.message = ""

    def move(self, delta: int) -> None:
        if self.editing:
            return
        items = self.items()
        self.item_index = (self.item_index + delta) % len(items)

    # -- actions ------------------------------------------------------------

    def activate(self) -> None:
        """Enter on the selected setting: toggle / cycle / begin editing."""
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
        ok, msg = self._apply(spec.key, raw)
        self.message = msg
        self.message_ok = ok

    # -- rendering ----------------------------------------------------------

    def _value_text(self, spec: Setting) -> tuple[str, str]:
        """(style, text) for a setting's value (friendly, non-selected rows)."""
        if spec.type == "bool":
            return (_C_ON, "● On") if bool(self.value_of(spec)) else (_C_DIM, "○ Off")
        val = self.value_of(spec)
        if val is None or val == "":
            return (_C_DIM, "—")
        if spec.type == "enum":
            return (_C_ACCENT, str(val))
        return ("", str(val))

    def render_lines(self) -> list[tuple[str, str]]:
        """(style, text) fragments for a prompt_toolkit FormattedTextControl.

        Layout: a title, horizontal category tabs, the active category's settings
        (human label + value, aligned, selected row highlighted), then a detail
        box describing the *selected* setting — so the screen stays uncluttered
        but always explains what the highlighted thing does.
        """
        out: list[tuple[str, str]] = [("bold", "  ⚙  Settings"), (_C_DIM, "   esc to close\n\n")]

        # Horizontal category tabs.
        out.append(("", "   "))
        for i, g in enumerate(self.groups):
            style = "reverse bold" if i == self.cat_index else _C_DIM
            out.append((style, f" {g} "))
            out.append(("", " "))
        out.append(("", "\n\n"))

        # Settings in the active category — label column aligned to the value.
        items = self.items()
        width = max((len(s.label) for s in items), default=0)
        for i, spec in enumerate(items):
            selected = i == self.item_index
            label = spec.label.ljust(width)
            marker = "▸ " if selected else "  "
            if selected and self.editing:
                out.append(("reverse", f"   {marker}{label}   {self.buffer}▏ \n"))
            elif selected:
                _vs, vtext = self._value_text(spec)
                out.append(("reverse", f"   {marker}{label}   {vtext} \n"))
            else:
                vstyle, vtext = self._value_text(spec)
                out.append(("", f"   {marker}{label}   "))
                out.append((vstyle, vtext))
                out.append(("", "\n"))

        # Detail box for the selected setting: its description + how to change it.
        spec = self.current()
        out.append((_C_DIM, "\n   " + "─" * (width + 28) + "\n"))
        out.append(("bold", f"   {spec.label}  "))
        out.append((_C_DIM, f"{spec.desc}\n"))
        if self.editing:
            hint = "type a value · Enter save · Esc cancel"
        elif spec.type == "bool":
            hint = "Enter to toggle On/Off"
        elif spec.type == "enum":
            opts = " · ".join(c if c else "(none)" for c in spec.choices)
            hint = f"Enter to cycle:  {opts}"
        else:
            hint = "Enter to edit"
        out.append((_C_ACCENT, f"   {hint}\n"))
        out.append((_C_DIM, "   ←/→ switch section · ↑/↓ move\n"))
        if self.message and not self.editing:
            if not self.message_ok:
                style, glyph = _C_ERR, "✗"
            elif "⚠" in self.message:
                style, glyph = _C_WARN, ""   # message already carries its own ⚠
            else:
                style, glyph = _C_ON, "✓"
            prefix = f"{glyph} " if glyph else ""
            out.append((style, f"   {prefix}{self.message}\n"))
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
