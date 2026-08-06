"""Public surface checks for ``jarn.agent``."""

from __future__ import annotations

import jarn.agent


def test_approver_exported() -> None:
    assert hasattr(jarn.agent, "Approver") is True
