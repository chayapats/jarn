"""Persist ``always``-allow permission rules to the project config.

When a user approves an action with ``RememberScope.ALWAYS`` the rule must
survive across processes, not just the session. The :class:`PermissionEngine`
calls :meth:`PermissionRuleStore.add_allow` (injected as ``engine.persist``) to
append the rule to ``<project>/.jarn/config.yaml`` under ``permissions.allow``.

The file is round-tripped with ``ruamel.yaml`` so a user's comments, key order,
and formatting in ``config.yaml`` are preserved across the edit, and written
atomically (tmp + ``os.replace``).
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


def _yaml() -> YAML:
    y = YAML()  # round-trip mode: preserves comments + style
    y.preserve_quotes = True
    return y


class PermissionRuleStore:
    """Appends allow-rules to a project ``config.yaml`` (atomic, idempotent,
    comment-preserving)."""

    def __init__(self, config_path: Path | None) -> None:
        #: ``None`` when running outside a project — persistence is then a no-op.
        self.config_path = config_path

    def add_allow(self, rule: str) -> bool:
        """Append ``rule`` to ``permissions.allow``. Returns True if written.

        No-op (returns False) when there is no project config path or the rule
        is already present.
        """
        if self.config_path is None or not rule:
            return False
        data = self._load()
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
            data["permissions"] = perms
        allow = perms.get("allow")
        if not isinstance(allow, list):
            allow = list(allow) if allow else []
            perms["allow"] = allow
        if rule in allow:
            return False
        allow.append(rule)
        self._atomic_write(data)
        return True

    def _load(self) -> dict:
        path = self.config_path
        if path is None or not path.is_file():
            return {}
        try:
            loaded = _yaml().load(path.read_text(encoding="utf-8"))
        except (OSError, YAMLError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _atomic_write(self, data: dict) -> None:
        path = self.config_path
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        _yaml().dump(data, buf)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        os.replace(tmp, path)
