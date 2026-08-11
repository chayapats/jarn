"""Permission subsystem — modes, rules, danger-guard, remembered approvals."""

from jarn.permissions.engine import (
    Action,
    ActionKind,
    Decision,
    PermissionEngine,
    PermissionResult,
    RememberScope,
)
from jarn.permissions.guard import GuardLevel, GuardVerdict, inspect_command
from jarn.permissions.labels import (
    PERMISSION_MODE_DESCRIPTIONS,
    PERMISSION_MODE_NAMES,
    permission_mode_name,
    permission_mode_summary,
)

__all__ = [
    "Action",
    "ActionKind",
    "Decision",
    "GuardLevel",
    "GuardVerdict",
    "PermissionEngine",
    "PermissionResult",
    "PERMISSION_MODE_DESCRIPTIONS",
    "PERMISSION_MODE_NAMES",
    "RememberScope",
    "inspect_command",
    "permission_mode_name",
    "permission_mode_summary",
]
