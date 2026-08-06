"""``python -m jarn.telegram`` — delegates to the supported ``jarn gateway`` entry.

Prefer ``jarn gateway`` for operators. This module entry stays for smoke scripts
and backwards compatibility (same flags: ``--fake-backend``, env overrides).
"""

from __future__ import annotations

from jarn.telegram.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
