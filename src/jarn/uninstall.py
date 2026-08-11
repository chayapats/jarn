"""Itemized, data-preserving J.A.R.N. uninstall.

Project-local ``.jarn`` directories are never enumerated or touched.  The
interactive flow asks separately about the managed executable, exclusively-owned
runtime files, global configuration, sessions/user data, caches/logs, and
credentials.  Shared tools such as Node, Python, Codex CLI and uv are retained.
"""

from __future__ import annotations

import contextlib
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from jarn.config.secrets import (
    KeyringOperationError,
    delete_keychain_secret,
    redact_secrets,
)
from jarn.install_state import (
    InstallRecord,
    InstallStateError,
    load_actionable_install_record,
    validate_install_record_actions,
)

CATEGORY_ORDER = (
    "executable",
    "dependencies",
    "config",
    "sessions",
    "cache",
    "credentials",
)


@dataclass(frozen=True)
class UninstallItem:
    category: str
    label: str
    paths: tuple[Path, ...] = ()
    keychain_accounts: tuple[str, ...] = ()
    shared: bool = False


class _UninstallPromptCancelled(Exception):
    """Internal signal: an uninstall confirmation ended before mutation."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _dir_bytes(path: Path) -> int:
    """Return total byte size of *path* (0 when absent or inaccessible)."""
    if not (path.exists() or path.is_symlink()):
        return 0
    if path.is_file() or path.is_symlink():
        with contextlib.suppress(OSError):
            return path.lstat().st_size
        return 0
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file() or child.is_symlink():
                with contextlib.suppress(OSError):
                    total += child.lstat().st_size
    except OSError:
        pass
    return total


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{n} B"


def _trust_entry_count(home: Path) -> int:
    import yaml

    path = home / "trust.yaml"
    if not path.is_file():
        return 0
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return len(value) if isinstance(value, dict) else 0
    except Exception:  # noqa: BLE001 - summary only; target is still shown
        return 0


def _keychain_candidates() -> list[str]:
    from jarn.config.defaults import ALL_PROVIDERS

    return list(ALL_PROVIDERS)


def _load_record_best_effort() -> InstallRecord | None:
    from jarn.install_state import default_manifest_path

    manifest = default_manifest_path()
    try:
        return load_actionable_install_record(manifest)
    except InstallStateError:
        # Absence means an unmanaged install.  A present-but-unsafe receipt is
        # materially different: suppressing it would let uninstall print a
        # misleading completion while ignoring a tampered action target.
        if manifest.exists() or manifest.is_symlink():
            raise
        return None


def _channel_hint(
    frozen: bool | None = None,
    *,
    record: InstallRecord | None = None,
) -> str:
    """Return a non-destructive command for an unmanaged/legacy package."""
    if record is not None:
        if record.method in {"binary", "python", "existing"}:
            return f"managed executable recorded at {record.active_path}"
        if record.method == "npm":
            return "npm uninstall -g jarn-cli"
        if record.method in {"pip", "pipx", "uv"}:
            return f"{record.method} uninstall jarn"
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    return "npm uninstall -g jarn-cli" if frozen else "pip uninstall jarn"


def _unique_existing(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        expanded = path.expanduser()
        if expanded in seen:
            continue
        seen.add(expanded)
        if expanded.exists() or expanded.is_symlink():
            result.append(expanded)
    return tuple(result)


def _owned_runtime_paths(record: InstallRecord | None) -> list[Path]:
    """Return only isolated version roots proven to sit below state/versions."""
    if record is None or record.state_dir is None or record.candidate_path is None:
        return []
    versions = record.state_dir / "versions"
    candidate = record.candidate_path
    try:
        relative = candidate.relative_to(versions)
    except ValueError:
        return []
    if len(relative.parts) < 2:
        return []
    # Python fallback candidate: <versions>/<unique-root>/bin/jarn.  The first
    # component is installer-owned; uv itself lives elsewhere and is shared.
    return [versions / relative.parts[0]]


def build_uninstall_plan(
    *,
    home: Path | None = None,
    record: InstallRecord | None = None,
) -> list[UninstallItem]:
    """Build the exact removal inventory without mutating anything."""
    from jarn.config import paths

    root = home or paths.global_home()
    record = record if record is not None else _load_record_best_effort()
    if record is not None:
        manifest = record.state_dir / "install.json" if record.state_dir is not None else None
        validate_install_record_actions(record, manifest_path=manifest)
    executable_paths: list[Path] = []
    if record is not None:
        executable_paths.append(record.active_path)
        if record.previous_path is not None:
            executable_paths.append(record.previous_path)

    config_paths = [
        root / "config.yaml",
        root / "config.yaml.bak",
        root / "config.yaml.bak.1",
        root / "trust.yaml",
        root / "pricing.yaml",
        root / "context_windows.yaml",
        root / "commands",
        root / "skills",
        root / "agents",
    ]
    session_paths = [
        root / "state.sqlite",
        root / "state.sqlite-wal",
        root / "state.sqlite-shm",
        root / "sessions",
        root / "memory",
        root / "wiki",
        root / "personal",
        root / "history",
    ]
    cache_paths = [
        root / "cache",
        root / "logs",
        root / "run",
        root / "state",
        root / "update-check.json",
        root / "telemetry.jsonl",
        root / ".install_id",
    ]
    # platformdirs may place this outside ~/.jarn (macOS/Windows especially).
    # Include that external location only when operating on the canonical home;
    # a JARN_HOME override is a deliberately isolated scope and must not reach
    # back into the user's default cache tree.
    with contextlib.suppress(Exception):
        if root.resolve() == paths.default_global_home().resolve():
            cache_paths.append(paths.cachedir())

    dependency_paths = _owned_runtime_paths(record)
    state_paths: list[Path] = []
    if record is not None and record.state_dir is not None:
        state_paths.extend(
            [
                record.state_dir / "install.json",
                record.state_dir / "install.lock",
            ]
        )
        with contextlib.suppress(OSError):
            state_paths.extend(record.state_dir.glob("install-*.log"))
    executable_paths.extend(state_paths)

    return [
        UninstallItem(
            "executable",
            "managed J.A.R.N. command, retained rollback binary, and install record",
            _unique_existing(executable_paths),
        ),
        UninstallItem(
            "dependencies",
            "isolated Python tool environments owned exclusively by J.A.R.N.",
            _unique_existing(dependency_paths),
        ),
        UninstallItem(
            "config",
            f"global configuration and trust ({_trust_entry_count(root)} trust entries)",
            _unique_existing(config_paths),
        ),
        UninstallItem(
            "sessions",
            "global sessions, transcripts, memory, wiki, and prompt history",
            _unique_existing(session_paths),
        ),
        UninstallItem(
            "cache",
            "regenerable caches, logs, runtime files, and local telemetry",
            _unique_existing(cache_paths),
        ),
        UninstallItem(
            "credentials",
            "J.A.R.N. secret files and keychain entries (Codex login remains scoped to Codex)",
            _unique_existing([root / "secrets"]),
            tuple(_keychain_candidates()),
        ),
    ]


def _print_plan(items: list[UninstallItem], *, home: Path, record: InstallRecord | None) -> None:
    total = sum(_dir_bytes(path) for item in items for path in item.paths)
    print(f"\nJ.A.R.N. uninstall inventory ({_human_size(total)})")
    print(f"Global user-data root: {home}")
    for item in items:
        print(f"\n  [{item.category}] {item.label}")
        if not item.paths and not item.keychain_accounts:
            print("    (nothing detected)")
        for path in item.paths:
            print(f"    - {path} ({_human_size(_dir_bytes(path))})")
        if item.keychain_accounts:
            print(f"    - keychain: {len(item.keychain_accounts)} jarn/<provider> entries")
    print("\nShared Codex CLI, uv, Node and Python installations are preserved.")
    if record is None:
        print(f"Legacy/unmanaged executable: inspect and run `{_channel_hint()}` separately.")


def _prompt_remove_category(item: UninstallItem, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    try:
        answer = input(f"Remove {item.category}? [{suffix}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt) as exc:
        raise _UninstallPromptCancelled(item.category) from exc
    return (not answer and default) or answer in {"y", "yes"}


def _ask_categories(items: list[UninstallItem]) -> set[str]:
    selected: set[str] = set()
    defaults = {"executable": True}
    for item in items:
        if _prompt_remove_category(item, default=defaults.get(item.category, False)):
            selected.add(item.category)
    return selected


def _confirm_categories(items: list[UninstallItem], categories: set[str]) -> set[str]:
    """Confirm only explicitly named categories, still before any mutation."""

    selected: set[str] = set()
    for item in items:
        if item.category in categories and _prompt_remove_category(item, default=False):
            selected.add(item.category)
    return selected


def _render_cancelled_uninstall(*, cause: str) -> int:
    from jarn.errors import ErrorCode, error_detail
    from jarn.exit_codes import EXIT_CANCELLED

    print(
        error_detail(
            ErrorCode.CANCELLED,
            "Uninstall was cancelled before removal began.",
            cause=cause,
            component="itemized uninstall confirmation",
            retryable=True,
            action=(
                "Nothing was removed. Re-run `jarn uninstall` and explicitly confirm "
                "only the intended categories."
            ),
        ).render(),
        file=sys.stderr,
    )
    return EXIT_CANCELLED


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def run_uninstall(
    *,
    yes: bool = False,
    frozen: bool | None = None,
    categories: set[str] | None = None,
) -> int:
    """Run the itemized uninstall flow.

    ``--yes`` skips confirmation but does not broaden scope: with no category
    flags it selects only the managed executable. Automation that intentionally
    removes user data must name every desired category explicitly. The normal
    interactive flow likewise defaults only the executable to **yes**.
    """
    from jarn.config import paths
    from jarn.errors import error_detail

    home = paths.global_home()
    try:
        record = _load_record_best_effort()
        items = build_uninstall_plan(home=home, record=record)
    except InstallStateError as exc:
        print(
            error_detail(
                "JARN-UNINSTALL-003",
                "Uninstall refused an unsafe install receipt.",
                cause=redact_secrets(str(exc)),
                component="managed installation metadata",
                retryable=False,
                action=(
                    "Run `jarn doctor --report jarn-support-report.json`, then repair "
                    "with the official curl installer before retrying uninstall."
                ),
            ).render(),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - this is the user-facing safety boundary
        safe_cause = redact_secrets(str(exc) or type(exc).__name__)
        print(
            error_detail(
                "JARN-UNINSTALL-002",
                "The uninstall inventory could not be built safely.",
                cause=safe_cause,
                component="itemized uninstall inventory",
                retryable=True,
                action=(
                    "Nothing was removed. Correct the reported filesystem or backend "
                    "problem, run `jarn doctor --report jarn-support-report.json`, and retry."
                ),
            ).render(),
            file=sys.stderr,
        )
        return 1
    _print_plan(items, home=home, record=record)

    unknown = (categories or set()) - set(CATEGORY_ORDER)
    if unknown:
        print(
            error_detail(
                "JARN-UNINSTALL-001",
                "The uninstall category selection is invalid.",
                cause=redact_secrets(f"Unknown categories: {', '.join(sorted(unknown))}."),
                component="uninstall selection",
                retryable=False,
                action=(
                    "Choose only executable, dependencies, config, sessions, cache, "
                    "or credentials, then retry."
                ),
            ).render(),
            file=sys.stderr,
        )
        return 2
    if yes:
        selected = set(categories) if categories is not None else {"executable"}
    else:
        try:
            selected = (
                _confirm_categories(items, categories)
                if categories is not None
                else _ask_categories(items)
            )
        except _UninstallPromptCancelled as exc:
            return _render_cancelled_uninstall(
                cause=f"The confirmation for category {exc.category!r} was interrupted or closed."
            )

    if not selected:
        return _render_cancelled_uninstall(cause="No uninstall category was confirmed for removal.")

    errors: list[str] = []
    for item in items:
        if item.category not in selected:
            continue
        for path in item.paths:
            try:
                _remove_path(path)
                print(f"Removed {path}")
            except OSError as exc:
                errors.append(redact_secrets(f"{path}: {exc}"))
        if item.category == "credentials":
            for account in item.keychain_accounts:
                try:
                    delete_keychain_secret("jarn", account, timeout=2.0)
                except KeyringOperationError as exc:
                    if exc.error_type == "PasswordDeleteError":
                        continue
                    errors.append(redact_secrets(f"keychain jarn/{account}: {exc}"))
                except TimeoutError as exc:
                    errors.append(redact_secrets(f"keychain jarn/{account}: {exc}"))
                    # A blocked backend will block every account. The worker was
                    # reaped; stop here instead of charging the timeout repeatedly.
                    break
                except Exception as exc:  # noqa: BLE001 - backend failure is material
                    errors.append(redact_secrets(f"keychain jarn/{account}: {exc}"))

    # Remove now-empty owned containers only; never recursively remove the home.
    candidates = [home]
    if record is not None and record.state_dir is not None:
        candidates.extend([record.state_dir / "versions", record.state_dir])
    for directory in candidates:
        with contextlib.suppress(OSError):
            directory.rmdir()

    if errors:
        print("\nUninstall incomplete:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            error_detail(
                "JARN-UNINSTALL-002",
                "One or more selected uninstall items could not be removed.",
                cause="; ".join(errors[:10]),
                component="itemized uninstall",
                retryable=True,
                action=(
                    "Correct the reported permissions/backend problem and retry the same "
                    "categories; no unlisted path was removed."
                ),
            ).render(),
            file=sys.stderr,
        )
        return 1

    kept = [category for category in CATEGORY_ORDER if category not in selected]
    print("\nUninstall completed for the selected categories.")
    if kept:
        print(f"Preserved: {', '.join(kept)}.")
    if record is None:
        print(f"If a legacy package command remains, inspect then run: {_channel_hint(frozen)}")
    return 0
