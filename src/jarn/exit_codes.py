"""Stable process exit codes for the public J.A.R.N. CLI.

The numeric values are part of the automation contract.  Add aliases when a
name changes; do not silently reuse a number for a different failure class.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Version-1 CLI exit-code taxonomy."""

    SUCCESS = 0
    INTERNAL = 1
    USAGE_CONFIG = 2
    AUTH = 3
    MODEL_UNAVAILABLE = 4
    PERMISSION_DENIED = 5
    NETWORK_PROVIDER = 6
    UPDATE_FAILED = 7
    BUDGET_EXCEEDED = 8
    VERIFICATION_FAILED = 9
    TIMEOUT = 124
    CANCELLED = 130


EXIT_SUCCESS = int(ExitCode.SUCCESS)
EXIT_INTERNAL = int(ExitCode.INTERNAL)
EXIT_USAGE_CONFIG = int(ExitCode.USAGE_CONFIG)
EXIT_AUTH = int(ExitCode.AUTH)
EXIT_MODEL_UNAVAILABLE = int(ExitCode.MODEL_UNAVAILABLE)
EXIT_PERMISSION_DENIED = int(ExitCode.PERMISSION_DENIED)
EXIT_NETWORK_PROVIDER = int(ExitCode.NETWORK_PROVIDER)
EXIT_UPDATE_FAILED = int(ExitCode.UPDATE_FAILED)
EXIT_BUDGET_EXCEEDED = int(ExitCode.BUDGET_EXCEEDED)
EXIT_VERIFICATION_FAILED = int(ExitCode.VERIFICATION_FAILED)
EXIT_TIMEOUT = int(ExitCode.TIMEOUT)
EXIT_CANCELLED = int(ExitCode.CANCELLED)


__all__ = [
    "EXIT_AUTH",
    "EXIT_BUDGET_EXCEEDED",
    "EXIT_CANCELLED",
    "EXIT_INTERNAL",
    "EXIT_MODEL_UNAVAILABLE",
    "EXIT_NETWORK_PROVIDER",
    "EXIT_PERMISSION_DENIED",
    "EXIT_SUCCESS",
    "EXIT_TIMEOUT",
    "EXIT_UPDATE_FAILED",
    "EXIT_USAGE_CONFIG",
    "EXIT_VERIFICATION_FAILED",
    "ExitCode",
]
