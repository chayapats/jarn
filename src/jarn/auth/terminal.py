"""Terminal-facing helpers for the unified ChatGPT authentication service.

The protocol service intentionally has no UI opinions.  This module supplies
the small amount of policy every terminal surface should share: choose a
device-code ceremony on remote/headless hosts, render the challenge *before*
waiting, and only report success after :class:`CodexAuthService` verifies the
account.
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from collections.abc import Mapping
from contextlib import suppress
from typing import TextIO

from rich.console import Console

from jarn.auth.models import AuthStatus, LoginChallenge, LoginMethod
from jarn.auth.service import CodexAuthService
from jarn.tui import layout


def detect_login_method(
    *,
    force_device: bool = False,
    force_browser: bool = False,
    environ: Mapping[str, str] | None = None,
    stdin_isatty: bool | None = None,
    stdout_isatty: bool | None = None,
    platform: str | None = None,
) -> LoginMethod:
    """Choose browser locally and device-code on SSH/headless/CI hosts.

    Explicit flags always win.  ``stdin_isatty`` / ``stdout_isatty`` and the
    environment are injectable so installers and tests can exercise the exact
    decision without mutating process-global state.
    """

    if force_device and force_browser:
        raise ValueError("--device and --browser cannot be used together")
    if force_device:
        return LoginMethod.DEVICE_CODE
    if force_browser:
        return LoginMethod.BROWSER

    env = os.environ if environ is None else environ
    stdin_tty = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
    stdout_tty = sys.stdout.isatty() if stdout_isatty is None else stdout_isatty
    system = sys.platform if platform is None else platform

    remote = any(env.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))
    automated = any(
        env.get(name)
        for name in (
            "CI",
            "CODESPACES",
            "GITPOD_WORKSPACE_ID",
            "container",
            "KUBERNETES_SERVICE_HOST",
        )
    )
    no_terminal = not (stdin_tty and stdout_tty)
    linux_without_desktop = (
        system.startswith("linux") and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY")
    )
    if remote or automated or no_terminal or linux_without_desktop:
        return LoginMethod.DEVICE_CODE
    return LoginMethod.BROWSER


def render_login_challenge(
    challenge: LoginChallenge,
    *,
    console: Console,
    as_json: bool = False,
    open_browser: bool = True,
    json_stream: TextIO | None = None,
) -> None:
    """Print the actionable URL/code before the caller begins waiting."""

    if as_json:
        stream = json_stream or sys.stdout
        print(
            json.dumps(
                {"type": "auth_challenge", **challenge.to_dict()},
                ensure_ascii=False,
            ),
            file=stream,
            flush=True,
        )
        return

    if challenge.method is LoginMethod.DEVICE_CODE:
        console.print("\n" + layout.accent("Sign in to ChatGPT", bold=True))
        console.print(f"1. Open this link on any device:\n   {layout.link(challenge.url)}")
        if challenge.user_code:
            console.print(
                f"2. Enter this one-time code: {layout.warn(challenge.user_code)}"
            )
    else:
        # Always print the fallback URL.  Browser launch is best effort and can
        # silently fail in minimal desktop environments.
        console.print("\n" + layout.accent("Sign in to ChatGPT", bold=True))
        console.print(f"Open this link in your browser:\n  {layout.link(challenge.url)}")
        if open_browser:
            with suppress(OSError, webbrowser.Error):
                webbrowser.open(challenge.url)
    if challenge.expires_in_seconds:
        console.print(
            layout.muted(f"This sign-in challenge expires in {challenge.expires_in_seconds}s.")
        )
    console.print(layout.muted("Waiting for sign-in… Press Ctrl+C to cancel."))


def login_interactive(
    service: CodexAuthService,
    *,
    method: LoginMethod,
    console: Console,
    as_json: bool = False,
    open_browser: bool = True,
) -> AuthStatus:
    """Run the shared visible terminal ceremony and return verified status."""

    def progress(stage: str) -> None:
        if as_json:
            print(
                json.dumps(
                    {"schema_version": 1, "type": "auth_progress", "stage": stage},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        elif stage == "verifying_account":
            console.print(layout.muted("Sign-in received; verifying the ChatGPT account…"))

    return service.login(
        method,
        on_challenge=lambda challenge: render_login_challenge(
            challenge,
            console=console,
            as_json=as_json,
            open_browser=open_browser,
        ),
        on_progress=progress,
    )


__all__ = ["detect_login_method", "login_interactive", "render_login_challenge"]
