"""Shared UI building blocks for J.A.R.N.

The full-screen Textual chat app has been retired in favour of the terminal
front-end (:mod:`jarn.repl`). What remains here is UI-agnostic and reused by it:
the :class:`~jarn.tui.controller.Controller`, completion, palette, logo, the
edit-diff renderer, and the ``theme``/``keys``/``keyfix`` helpers (the last of
which are also used by the onboarding wizard).
"""
