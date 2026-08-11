"""Verified, non-secret completion summary shared by both setup surfaces."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from jarn.auth import AuthStatus
from jarn.config.defaults import CLOUD_PROVIDERS
from jarn.install_state import (
    InstallStateError,
    default_manifest_path,
    load_actionable_install_record,
)
from jarn.permissions.labels import permission_mode_summary

_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")


class SetupCompletionError(RuntimeError):
    """The final runtime identity could not be verified."""


@dataclass(frozen=True, slots=True)
class InstallIdentity:
    executable: str
    version: str
    method: str
    verified: bool = True


@dataclass(frozen=True, slots=True)
class SetupCompletion:
    install: InstallIdentity
    config_path: Path
    backup_path: Path | None
    provider: str
    model: str
    model_display: str
    reasoning_effort: str | None
    permission_mode: str
    cwd: Path
    auth_mode: str | None = None
    auth_plan: str | None = None
    auth_workspace: str | None = None
    validation: str = "not required"
    next_command: str = "jarn"


def _smoke_version(path: Path) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable and argument, no shell
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupCompletionError(f"could not run {path} --version: {exc}") from exc
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    match = _VERSION_RE.search(output)
    if completed.returncode != 0 or match is None:
        raise SetupCompletionError(
            f"{path} --version was not usable: {output or f'exit {completed.returncode}'}"
        )
    return match.group(1)


def verify_install_identity(*, manifest_path: Path | None = None) -> InstallIdentity:
    """Verify the active command and install method used in the final summary."""

    manifest = manifest_path or default_manifest_path()
    record = None
    if manifest.exists():
        try:
            record = load_actionable_install_record(manifest)
        except InstallStateError as exc:
            raise SetupCompletionError(
                f"managed install record {manifest} is not safe/actionable: {exc}"
            ) from exc
    if record is not None:
        version = _smoke_version(record.active_path)
        if version != record.version:
            raise SetupCompletionError(
                f"active executable reports {version}, but install record expects {record.version}"
            )
        resolved = shutil.which("jarn")
        if resolved is None:
            raise SetupCompletionError(
                "the managed executable is healthy, but ordinary `jarn` does not resolve on "
                "PATH. Activate the installer-provided shell profile, then retry `jarn setup`."
            )
        try:
            resolved_path = Path(resolved).resolve(strict=True)
            active_path = record.active_path.resolve(strict=True)
        except OSError as exc:
            raise SetupCompletionError(
                f"could not verify the user-visible `jarn` command against the install record: {exc}"
            ) from exc
        if resolved_path != active_path:
            raise SetupCompletionError(
                "the managed executable is healthy, but ordinary `jarn` resolves to "
                f"{resolved_path} instead of {active_path}. Remove or reorder the shadowing "
                "installation, refresh the shell command cache, then retry `jarn setup`."
            )
        return InstallIdentity(
            executable=str(record.active_path),
            version=version,
            method=record.method,
        )

    resolved = shutil.which("jarn")
    if resolved:
        path = Path(resolved).absolute()
        return InstallIdentity(
            executable=str(path),
            version=_smoke_version(path),
            method="unmanaged/Python package",
        )

    import sys

    argv0 = Path(sys.argv[0]).expanduser()
    if "jarn" in argv0.name.lower() and argv0.is_file() and os.access(argv0, os.X_OK):
        return InstallIdentity(
            executable=str(argv0.absolute()),
            version=_smoke_version(argv0),
            method="unmanaged current entrypoint",
        )

    raise SetupCompletionError(
        "the active `jarn` executable could not be resolved for the completion "
        "summary. Activate the installed command on PATH and retry `jarn setup`."
    )


def completion_from_setup(
    *,
    install: InstallIdentity,
    config_path: Path,
    backup_path: Path | None,
    provider: str,
    model: str,
    model_display: str | None,
    reasoning_effort: str | None,
    permission_mode: str,
    auth: AuthStatus | None,
    validation: str,
    cwd: Path | None = None,
) -> SetupCompletion:
    workspace = None
    if auth is not None and auth.workspace is not None:
        workspace = auth.workspace.name or (
            f"id {auth.workspace.id_hash}" if auth.workspace.id_hash else None
        )
    return SetupCompletion(
        install=install,
        config_path=config_path,
        backup_path=backup_path,
        provider="ChatGPT subscription" if provider == "codex_subscription" else provider,
        model=model,
        model_display=model_display or model,
        reasoning_effort=reasoning_effort,
        permission_mode=permission_mode,
        cwd=(cwd or Path.cwd()).resolve(),
        auth_mode=(
            auth.auth_mode
            if auth is not None
            else ("API key reference" if provider in CLOUD_PROVIDERS else "none (local)")
        ),
        auth_plan=auth.plan_type if auth is not None else None,
        auth_workspace=workspace,
        validation=validation,
    )


def render_setup_completion(console: Console, summary: SetupCompletion) -> None:
    """Render only facts verified before the successful commit."""

    install_label = "verified" if summary.install.verified else "unverified"
    console.print("\n[bold green]Setup complete.[/bold green]")
    console.print(
        f"  J.A.R.N.: [b]{escape(summary.install.version)}[/b] · "
        f"{escape(summary.install.method)} · {install_label}"
    )
    console.print(f"  Executable: [b]{escape(summary.install.executable)}[/b]")
    console.print(f"  Config: [b]{escape(str(summary.config_path))}[/b]")
    if summary.backup_path is not None:
        console.print(f"  Previous config backup: {escape(str(summary.backup_path))}")
    console.print(f"  Provider: [b]{escape(summary.provider)}[/b]")
    if summary.auth_mode:
        console.print(f"  Authentication: [b]{escape(summary.auth_mode)}[/b]")
    if summary.auth_plan:
        console.print(f"  ChatGPT plan: [b]{escape(summary.auth_plan)}[/b]")
    if summary.auth_workspace:
        console.print(f"  Workspace: [b]{escape(summary.auth_workspace)}[/b]")
    console.print(
        f"  Model: [b]{escape(summary.model_display)}[/b] [dim]({escape(summary.model)})[/dim]"
    )
    if summary.reasoning_effort:
        console.print(f"  Reasoning: [b]{escape(summary.reasoning_effort)}[/b]")
    console.print(
        f"  Permission: [b]{escape(permission_mode_summary(summary.permission_mode))}[/b]"
    )
    console.print(f"  Working directory: [b]{escape(str(summary.cwd))}[/b]")
    console.print(f"  Provider validation: {escape(summary.validation)}")
    console.print(f"\n  Next command: [bold cyan]{escape(summary.next_command)}[/bold cyan]")


__all__ = [
    "InstallIdentity",
    "SetupCompletion",
    "SetupCompletionError",
    "completion_from_setup",
    "render_setup_completion",
    "verify_install_identity",
]
