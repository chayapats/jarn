"""Frozen-binary entry point for PyInstaller builds."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    # Frozen builds re-execute this binary for killable keyring/model workers.
    # Selectors are non-secret; complete requests (including credentials) arrive
    # on stdin. Dispatch before importing the CLI so workers cannot recurse into
    # normal argument parsing or appear in user-facing help.
    internal_selector = sys.argv[1] if len(sys.argv) == 2 else ""
    if internal_selector.startswith("__jarn_internal_"):
        from jarn.config.secrets import (
            _KEYRING_WORKER_SELECTOR,
            _keyring_worker_main,
        )
        from jarn.onboarding.wizard import (
            _VALIDATION_WORKER_SELECTOR,
            _validation_worker_main,
        )

        if internal_selector == _KEYRING_WORKER_SELECTOR:
            raise SystemExit(_keyring_worker_main())
        if internal_selector == _VALIDATION_WORKER_SELECTOR:
            raise SystemExit(_validation_worker_main())

    from jarn.cli import main

    raise SystemExit(main())
