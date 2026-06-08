"""Project trust boundary.

A project's ``.jarn/config.yaml`` is *untrusted input*: opening a repository must
not, by itself, run code or leak secrets. Yet several config keys grant exactly
those capabilities when merely loaded —

* ``hooks`` — shell commands run automatically on lifecycle events (e.g.
  ``session_start`` fires before the user does anything).
* ``mcp_servers`` — stdio servers are *spawned* (arbitrary commands) at startup.
* ``async_subagents`` — remote graphs the agent can reach, with project-defined
  URLs/headers.
* ``providers`` — a project can point ``base_url`` at an attacker and reference
  ``${ANY_ENV}`` / ``keychain:*`` to exfiltrate a real secret on the next call.
* ``execution`` — backend choice (e.g. force ``local`` off a sandbox).
* ``permission_mode`` — a project could force ``yolo``.
* ``permissions.allow`` — pre-approve commands without the user ever seeing them.

So a project must be **explicitly trusted** before those keys take effect. Until
then they are stripped (the rest of the project config — UI theme, context
budget, deny rules — still applies). Trust is recorded per project root together
with a *fingerprint* of the dangerous subset, so adding a hook to an
already-trusted project re-triggers the prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jarn.config import paths

#: Top-level project keys that grant capability or can exfiltrate secrets.
DANGEROUS_TOP_KEYS = (
    "hooks",
    "mcp_servers",
    "async_subagents",
    "providers",
    "execution",
    "permission_mode",
)


def project_dangerous(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the capability-granting subset of a project config.

    Used to decide whether a trust prompt is needed and to fingerprint what the
    user is being asked to trust. ``permissions.allow`` is surfaced separately
    from the (safety-increasing) ``permissions.deny``.
    """
    danger: dict[str, Any] = {k: raw[k] for k in DANGEROUS_TOP_KEYS if k in raw}
    allow = (raw.get("permissions") or {}).get("allow")
    if allow:
        danger["permissions.allow"] = allow
    return danger


def sanitize_project(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop capability-granting keys so an untrusted project can't run code or
    leak secrets. Keeps benign keys and the safety-increasing ``permissions.deny``."""
    safe = {k: v for k, v in raw.items() if k not in DANGEROUS_TOP_KEYS}
    perms = safe.get("permissions")
    if isinstance(perms, dict) and "allow" in perms:
        trimmed = {k: v for k, v in perms.items() if k != "allow"}
        if trimmed:
            safe["permissions"] = trimmed
        else:
            safe.pop("permissions", None)
    return safe


def fingerprint(dangerous: dict[str, Any]) -> str:
    """Stable hash of the dangerous subset (order-independent)."""
    canonical = json.dumps(dangerous, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_project_trusted(root: Path, *, store: TrustStore | None = None) -> bool:
    """Return whether capability-granting project config may be honoured.

    Non-interactive: consults only the trust store (no prompt). When the project
    declares dangerous keys but is not trusted at the current fingerprint, returns
    ``False`` so callers can fail closed (e.g. ``jarn doctor``).
    """
    from jarn.config.loader import _read_yaml
    from jarn.config.paths import project_config_path

    danger = project_dangerous(_read_yaml(project_config_path(root)))
    if not danger:
        return True
    trust_store = store if store is not None else TrustStore.load()
    return trust_store.status(root, fingerprint(danger)) == "trusted"


@dataclass(slots=True)
class TrustStore:
    """Records which project roots have been trusted, and at what fingerprint."""

    path: Path
    _entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> TrustStore:
        p = path or (paths.global_home() / "trust.yaml")
        entries: dict[str, str] = {}
        if p.is_file():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    entries = {str(k): str(v) for k, v in data.items()}
            except yaml.YAMLError:
                entries = {}  # a corrupt store fails closed (everything untrusted)
        return cls(path=p, _entries=entries)

    def status(self, root: Path, fp: str) -> str:
        """``"trusted"`` (root trusted at this fingerprint), ``"changed"`` (trusted
        before but the dangerous config changed), or ``"untrusted"``."""
        known = self._entries.get(str(root.resolve()))
        if known is None:
            return "untrusted"
        return "trusted" if known == fp else "changed"

    def trust(self, root: Path, fp: str) -> None:
        self._entries[str(root.resolve())] = fp

    def untrust(self, root: Path) -> bool:
        """Forget a trusted root. Returns ``True`` if an entry was removed."""
        return self._entries.pop(str(root.resolve()), None) is not None

    def entries(self) -> dict[str, str]:
        """Return a copy of the ``{root: fingerprint}`` map (sorted by root)."""
        return {k: self._entries[k] for k in sorted(self._entries)}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(self._entries, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
