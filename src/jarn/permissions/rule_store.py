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

from pathlib import Path

from jarn.config.yaml_store import atomic_write_yaml, load_yaml_doc


class PermissionRuleStore:
    """Appends allow-rules to a project ``config.yaml`` (atomic, idempotent,
    comment-preserving).

    A corrupt project config is never silently wiped: :meth:`add_allow` raises
    :class:`ConfigCorruptError` (after saving a ``.bak``) instead of overwriting
    the file with a near-empty dict. A missing file bootstraps from ``{}``.
    """

    def __init__(self, config_path: Path | None) -> None:
        #: ``None`` when running outside a project — persistence is then a no-op.
        self.config_path = config_path

    def add_allow(self, rule: str) -> bool:
        """Append ``rule`` to ``permissions.allow``. Returns True if written.

        No-op (returns False) when there is no project config path or the rule
        is already present. Raises :class:`ConfigCorruptError` if the project
        config is unreadable (the file is left untouched and a ``.bak`` is saved).
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
        if self.config_path is None:
            return {}
        return load_yaml_doc(self.config_path)

    def _atomic_write(self, data: dict) -> None:
        assert self.config_path is not None
        atomic_write_yaml(self.config_path, data)
