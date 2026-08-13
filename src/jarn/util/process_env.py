"""Environment boundaries for child executables launched by J.A.R.N."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

_PYINSTALLER_RESET_ENVIRONMENT = "PYINSTALLER_RESET_ENVIRONMENT"


def external_command_env(
    base: Mapping[str, str] | None = None,
    *,
    frozen: bool | None = None,
    platform_name: str | None = None,
) -> dict[str, str]:
    """Return an environment safe for an independent external executable.

    PyInstaller one-file applications temporarily prepend their extraction
    directory to ``LD_LIBRARY_PATH`` and preserve the pre-bundle value in
    ``LD_LIBRARY_PATH_ORIG``.  An unrelated child such as Node/npm must not load
    J.A.R.N.'s bundled C/C++ libraries.  Restore the original search path (or
    remove it when no original value existed), then force any PyInstaller child
    to own a fresh extraction runtime.  Non-frozen executables ignore the reset
    flag.
    """

    env = dict(os.environ if base is None else base)
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    platform_value = sys.platform if platform_name is None else platform_name
    loader_path_is_rewritten = platform_value.startswith(("linux", "aix")) and (
        is_frozen or "_PYI_ARCHIVE_FILE" in env or "LD_LIBRARY_PATH_ORIG" in env
    )
    if loader_path_is_rewritten:
        original_loader_path = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original_loader_path is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original_loader_path
    env[_PYINSTALLER_RESET_ENVIRONMENT] = "1"
    return env
