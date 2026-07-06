"""Background update-available notice.

At interactive launch a daemon thread checks ``https://pypi.org/pypi/jarn/json``
(2 s timeout) and prints **one dim line** under the splash when a newer release
exists.  The result is cached in ``~/.jarn/update-check.json`` for 24 h.

Skip conditions (checked before starting the thread):
- ``updates.check: false`` in config
- ``offline`` preset active (``sandbox_allow_network`` is irrelevant; preset
  name is the authoritative signal passed from the CLI launch boundary)
- headless (``jarn -p``) mode — no splash, no notice

Silent on all network / parse errors; never blocks the first prompt.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from jarn.config.schema import Config

_PYPI_URL = "https://pypi.org/pypi/jarn/json"
_CACHE_SECS: float = 24.0 * 3600.0
_TIMEOUT: float = 2.0
_CHANGELOG_URL = "https://github.com/deepagents/jarn/releases"


def _get_install_cmd(frozen: bool | None = None) -> str:
    """Return the appropriate upgrade command for the running distribution."""
    import sys

    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    return "npm i -g jarn-cli" if frozen else "pip install -U jarn"


def _default_fetch() -> dict:  # type: ignore[type-arg]
    """GET the PyPI JSON endpoint and return the parsed dict."""
    import urllib.request

    with urllib.request.urlopen(_PYPI_URL, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())  # type: ignore[no-any-return]


def _do_update_check(
    *,
    home_dir: Path,
    current_version: str,
    now: float | None = None,
    frozen: bool | None = None,
    _fetch: Callable[[], dict] | None = None,  # type: ignore[type-arg]
) -> str | None:
    """Core update-check logic (synchronous, injectable for tests).

    Returns the notice string if a newer version exists, otherwise ``None``.
    On cache hit (< 24 h) skips HTTP entirely.  Silent on all errors.
    """
    if now is None:
        now = time.time()

    stamp_path = home_dir / "update-check.json"

    # ── 24 h cache ────────────────────────────────────────────────────────
    try:
        cached = json.loads(stamp_path.read_text(encoding="utf-8"))
        ts = float(cached.get("ts", 0))
        cached_latest = str(cached.get("latest", ""))
        if now - ts < _CACHE_SECS and cached_latest:
            from packaging.version import parse as vparse

            if vparse(cached_latest) > vparse(current_version):
                return (
                    f"⬆ v{cached_latest} available — "
                    f"{_get_install_cmd(frozen)} "
                    f"(changelog: {_CHANGELOG_URL})"
                )
            return None
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        pass  # corrupt / absent stamp → fall through to HTTP

    # ── HTTP fetch ────────────────────────────────────────────────────────
    fetch = _fetch if _fetch is not None else _default_fetch
    try:
        data = fetch()
        latest = str(data["info"]["version"])
    except Exception:  # noqa: BLE001 — silent on any failure
        return None

    # Write / update stamp (best-effort; read-only home must not crash)
    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(
            json.dumps({"ts": now, "latest": latest}),
            encoding="utf-8",
        )
    except OSError:
        pass

    from packaging.version import parse as vparse

    if vparse(latest) > vparse(current_version):
        return (
            f"⬆ v{latest} available — "
            f"{_get_install_cmd(frozen)} "
            f"(changelog: {_CHANGELOG_URL})"
        )
    return None


def maybe_start_update_check(
    config: Config,
    console: Console,
    *,
    preset_name: str | None = None,
    headless: bool = False,
) -> threading.Thread | None:
    """Launch the background update-check thread if conditions permit.

    Returns the :class:`threading.Thread` or ``None`` when skipped.  Callers
    may join the thread (with a short timeout) for testing; production code
    ignores the return value.
    """
    # Skip conditions: headless, check disabled, or offline preset.
    if headless or not config.updates.check or preset_name == "offline":
        return None

    from jarn.config import paths
    from jarn.tui import palette
    from jarn.version import __version__

    def _run() -> None:
        try:
            home = paths.global_home()
            notice = _do_update_check(
                home_dir=home,
                current_version=__version__,
            )
            if notice is not None:
                console.print(  # type: ignore[attr-defined]
                    f"[{palette.C_DIM}]{notice}[/{palette.C_DIM}]"
                )
        except Exception:  # noqa: BLE001 — never crash startup
            pass

    t = threading.Thread(target=_run, daemon=True, name="jarn-update-check")
    t.start()
    return t
