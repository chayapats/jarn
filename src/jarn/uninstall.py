"""``jarn uninstall`` — remove global ~/.jarn state and OS keychain entries.

Removes ONLY the global J.A.R.N. home directory (``~/.jarn`` by default, or
``$JARN_HOME``).  Project-local ``.jarn/`` directories that live under individual
project roots are **never touched** — they belong to those repos, not to jarn's
global install.

Keychain entries are enumerated from the known provider list (:data:`ALL_PROVIDERS`)
and deleted via ``keyring.delete_password("jarn", provider)``.  Missing entries and
any backend error are silently tolerated — a single unavailable entry must not abort
the whole uninstall.

After removal the command prints the appropriate package-manager uninstall line:
``npm uninstall -g jarn-cli`` for frozen (npm-installed) builds, or
``pip uninstall jarn`` for Python-package installs.
"""

from __future__ import annotations

import contextlib
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------


def _dir_bytes(path: Path) -> int:
    """Return total byte size of *path* (0 when absent or inaccessible)."""
    if not path.exists():
        return 0
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                with contextlib.suppress(OSError):
                    total += f.stat().st_size
    except OSError:
        pass
    return total


def _human_size(n: int) -> str:
    """Format *n* bytes as a human-readable string (e.g. ``4.2 MB``)."""
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n_f = n / 1024
        if n_f < 1024 or unit == "GB":
            return f"{n_f:.1f} {unit}"
        n = int(n_f)
    return f"{n} B"  # unreachable, satisfies type checkers


def _trust_entry_count(home: Path) -> int:
    """Return the number of entries in *home*/trust.yaml (0 on any error)."""
    import yaml  # imported here — trust read is best-effort

    trust_path = home / "trust.yaml"
    if not trust_path.is_file():
        return 0
    try:
        data = yaml.safe_load(trust_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            return len(data)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _keychain_candidates() -> list[str]:
    """Return the ordered list of provider names to attempt to delete from keychain."""
    from jarn.config.defaults import ALL_PROVIDERS

    return list(ALL_PROVIDERS)


def _channel_hint(frozen: bool | None = None) -> str:
    """Return the package-manager uninstall command appropriate for this install."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    return "npm uninstall -g jarn-cli" if frozen else "pip uninstall jarn"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_uninstall(*, yes: bool = False, frozen: bool | None = None) -> int:
    """Perform the uninstall flow.

    Parameters
    ----------
    yes:
        When ``True`` skip the confirmation prompt (``--yes`` flag).
    frozen:
        Override ``sys.frozen`` detection for the channel hint (tests only).
        Pass ``None`` (default) to use the real value.

    Returns
    -------
    int
        0 on success, 1 when the user declines the confirmation prompt.
    """
    from jarn.config import paths

    home = paths.global_home()
    providers = _keychain_candidates()
    n_keys = len(providers)
    n_trust = _trust_entry_count(home)
    size_str = _human_size(_dir_bytes(home))

    if not yes:
        # Print itemized summary and prompt for confirmation.
        print("\nThis will permanently remove:")
        print(f"  • {home}  ({size_str})")
        print(f"  • {n_keys} keychain entries  (jarn/<provider>)")
        print(f"  • {n_trust} trust-store entries")
        print()
        try:
            answer = input("Proceed? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    # --- Delete keychain entries (tolerate any per-entry failure) ---
    import keyring  # imported inside function so tests can monkeypatch easily

    for provider in providers:
        with contextlib.suppress(Exception):
            keyring.delete_password("jarn", provider)

    # --- Remove global home ---
    if home.exists():
        try:
            shutil.rmtree(home)
        except OSError as exc:
            print(f"Warning: could not fully remove {home}: {exc}")

    # --- Final message with package-manager hint ---
    hint = _channel_hint(frozen)
    print(f"\nDone. To complete uninstall: {hint}")

    return 0
