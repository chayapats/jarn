"""Privacy-preserving support reports for :mod:`jarn.doctor`.

The report is built from an explicit allowlist instead of recursively dumping
doctor diagnostics.  That distinction is intentional: the full diagnostics
contain local paths and may contain subprocess/provider error text, while a
support report is safe to attach to a public issue by default.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarn.config.secrets import redact_secrets, redact_structure
from jarn.errors import ErrorCode, JarnUserError, error_detail
from jarn.util.atomic import atomic_write_text

SUPPORT_REPORT_VERSION = 1

_FORBIDDEN_KEY = re.compile(
    r"(?:^|_)(?:api_?key|authorization|command|credential|file_?content|output|"
    r"password|path|prompt|secret|token)(?:$|_)",
    re.IGNORECASE,
)
# An absolute POSIX path begins at a string/punctuation boundary. Provider/model
# identifiers such as ``openrouter/anthropic`` do not match because their slash
# is preceded by an identifier character.
_UNIX_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9._~-])/(?!/)[^\s\"',}\]]+")
_WINDOWS_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_HOME_PATH = re.compile(r"(?<!\S)~[\\/]")


def _dependency_summary(raw: dict[str, Any] | None) -> dict[str, Any]:
    item = raw or {}
    return {
        "installed": bool(item.get("path")),
        "healthy": bool(item.get("ok")),
        "version": item.get("version"),
        "timed_out": bool(item.get("timed_out")),
    }


def _config_summary(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return {
        "status": raw.get("status"),
        "source_version": raw.get("source_version"),
        "target_version": raw.get("target_version"),
    }


def _safe_errors(diag: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in diag.get("errors") or []:
        if not isinstance(raw, dict):
            continue
        # Causes, actions, details, and log/report locations are intentionally
        # absent: each can carry local identifying data.  The stable code is the
        # useful correlation key for support.
        rows.append(
            {
                "code": raw.get("code"),
                "summary": raw.get("summary"),
                "component": raw.get("component"),
                "retryable": bool(raw.get("retryable")),
            }
        )
    return rows


def build_support_report(
    diag: dict[str, Any],
    *,
    known_secrets: set[str] | None = None,
) -> dict[str, Any]:
    """Return the strict, path-free support-report projection of *diag*.

    Prompt/module text, commands, file contents, raw exception messages, local
    paths, provider hosts, credential-source metadata, and secret references
    have no route into this object.
    """
    jarn = diag.get("jarn") or {}
    platform_diag = diag.get("platform") or {}
    python_diag = platform_diag.get("python") or {}
    deps = diag.get("dependencies") or {}
    codex = deps.get("codex") or {}
    protocol = codex.get("protocol") or {}
    install = diag.get("installation") or {}
    config = diag.get("configuration") or {}
    secret_store = diag.get("secrets") or {}
    catalog = diag.get("catalog") or {}
    route = diag.get("selected_route") or {}
    sandbox = diag.get("sandbox") or {}
    update = diag.get("update") or {}
    extension_counts = (diag.get("extensions") or {}).get("counts") or {}

    network_rows = []
    for raw in (diag.get("network") or {}).get("checks") or []:
        if isinstance(raw, dict):
            network_rows.append(
                {
                    "provider": raw.get("provider"),
                    "reachable": bool(raw.get("reachable")),
                    "timed_out": bool(raw.get("timed_out")),
                    "timeout_seconds": raw.get("timeout_seconds"),
                    "elapsed_ms": raw.get("elapsed_ms"),
                }
            )

    providers = []
    for raw in (diag.get("auth") or {}).get("providers") or []:
        if isinstance(raw, dict):
            providers.append(
                {
                    "provider": raw.get("name"),
                    "authenticated": bool(raw.get("authenticated")),
                }
            )

    report = {
        "report_version": SUPPORT_REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "healthy": bool(diag.get("ok")),
        "jarn": {
            "version": jarn.get("version"),
            "install_method": (jarn.get("install") or {}).get("method"),
            "executable_candidates": len(jarn.get("path_candidates") or []),
            "shadowed_executables": len(jarn.get("shadowed") or []),
        },
        "host": {
            "system": platform_diag.get("system"),
            "release": platform_diag.get("release"),
            "architecture": platform_diag.get("architecture"),
            "libc": platform_diag.get("libc"),
            "python": {
                "version": python_diag.get("version"),
                "managed": bool(python_diag.get("managed")),
            },
        },
        "shell": {
            "name": (diag.get("shell") or {}).get("name"),
            "profile_present": (diag.get("shell") or {}).get("profile_present"),
            "executable_available": bool(jarn.get("active_executable")),
        },
        "installation": {
            "writable": install.get("writable"),
            "free_bytes": install.get("free_bytes"),
            "directory_mode": install.get("directory_mode"),
        },
        "dependencies": {
            "uv": _dependency_summary(deps.get("uv")),
            "codex": {
                **_dependency_summary(codex),
                "protocol_compatible": bool(protocol.get("compatible")),
            },
        },
        "configuration": {
            "schema_current": config.get("schema_current"),
            "global": _config_summary(config.get("global")),
            "project": _config_summary(config.get("project")),
        },
        "local_store": {
            "present": bool(secret_store.get("store_present")),
            "file_count": secret_store.get("file_count"),
            "root_mode": secret_store.get("root_mode"),
            "permission_issue_count": len(secret_store.get("permission_issues") or []),
        },
        "provider_access": providers,
        "catalog": {
            "source": catalog.get("source"),
            "freshness": catalog.get("freshness"),
            "cache_present": bool(catalog.get("cache_present")),
            "cache_age_seconds": catalog.get("cache_age_seconds"),
        },
        "selected_route": {
            "model": route.get("model"),
            "available": bool(route.get("available")),
        },
        "workspace": {"trusted": (diag.get("workspace") or {}).get("trusted")},
        "sandbox": {
            "backend": sandbox.get("backend"),
            "available": bool(sandbox.get("available")),
            "mode": sandbox.get("mode"),
        },
        "network": {
            "checked": bool((diag.get("network") or {}).get("checked")),
            "checks": network_rows,
        },
        "update": {
            "channel": update.get("channel"),
            "checks_enabled": update.get("checks_enabled"),
        },
        "extension_counts": dict(extension_counts),
        "errors": _safe_errors(diag),
        "warning_count": len(diag.get("warnings") or []),
    }
    return redact_structure(report, known=known_secrets)


def support_report_json(
    diag: dict[str, Any],
    *,
    known_secrets: set[str] | None = None,
) -> str:
    """Serialize a support report deterministically for scanning/attachment."""
    return (
        json.dumps(
            build_support_report(diag, known_secrets=known_secrets),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _walk_forbidden_keys(value: Any, *, prefix: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            location = f"{prefix}.{key}" if prefix else key
            if _FORBIDDEN_KEY.search(key):
                findings.append(f"forbidden field: {location}")
            findings.extend(_walk_forbidden_keys(item, prefix=location))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_walk_forbidden_keys(item, prefix=f"{prefix}[{index}]"))
    return findings


def scan_support_report(
    text: str,
    *,
    known_secrets: set[str] | None = None,
) -> list[str]:
    """Return privacy/security findings for serialized report text.

    This is deliberately fail-closed and can be used independently by CI or a
    support-bundle command.  Findings name categories only and never echo the
    offending secret or local path.
    """
    findings: list[str] = []
    if redact_secrets(text, known=known_secrets) != text:
        findings.append("unredacted secret-shaped value")
    if _UNIX_LOCAL_PATH.search(text):
        findings.append("absolute POSIX path")
    if _WINDOWS_LOCAL_PATH.search(text):
        findings.append("absolute Windows path")
    if _HOME_PATH.search(text) or "file://" in text.lower():
        findings.append("local file location")
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        findings.append("invalid JSON")
    else:
        findings.extend(_walk_forbidden_keys(decoded))
    return list(dict.fromkeys(findings))


def write_support_report(
    diag: dict[str, Any],
    path: Path,
    *,
    known_secrets: set[str] | None = None,
) -> Path:
    """Scan then atomically write a mode-``0600`` support report."""
    destination = Path(path)
    if destination.is_symlink():
        raise JarnUserError(
            error_detail(
                ErrorCode.DOCTOR_REPORT_FAILED,
                "Support report destination is a symbolic link.",
                cause="Refusing a destination whose identity can change.",
                component="doctor report",
                retryable=False,
                action="Choose a new regular file in a directory you control.",
                report_path=destination,
            )
        )
    text = support_report_json(diag, known_secrets=known_secrets)
    findings = scan_support_report(text, known_secrets=known_secrets)
    if findings:
        raise JarnUserError(
            error_detail(
                ErrorCode.DOCTOR_REPORT_FAILED,
                "Support report failed its privacy scan.",
                cause=", ".join(findings),
                component="doctor report",
                retryable=False,
                action="Do not share the report; run doctor without --report.",
                report_path=destination,
            )
        )
    try:
        atomic_write_text(destination, text, mode=0o600 if os.name != "nt" else None)
    except OSError as exc:
        raise JarnUserError(
            error_detail(
                ErrorCode.DOCTOR_REPORT_FAILED,
                "Support report could not be written.",
                cause=str(exc),
                component="doctor report",
                retryable=True,
                action="Choose a writable destination and retry.",
                report_path=destination,
            )
        ) from exc
    return destination


__all__ = [
    "SUPPORT_REPORT_VERSION",
    "build_support_report",
    "scan_support_report",
    "support_report_json",
    "write_support_report",
]
