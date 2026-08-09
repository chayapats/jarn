"""Doctor diagnostic data collection (no rendering)."""

from __future__ import annotations

import os
import stat
from typing import Any


def collect_doctor(
    diag: dict,
    *,
    config: Any = None,
    project_root: Any = None,
    project_trusted: bool | None = None,
    extra_roots: Any = None,
    prompt_modules: dict[str, Any] | None = None,
) -> int:
    """Populate ``diag`` with doctor diagnostics and return the exit code.

    Pure data collection — no rendering — so the same diagnostics back both the
    Rich and the ``--json`` output paths.

    When ``config`` is provided (e.g. from the REPL controller), the function
    uses it directly instead of loading from disk.  ``project_root`` and
    ``project_trusted`` are also accepted so the caller can pass its live
    session state.
    """
    from jarn.config import paths
    from jarn.config.secrets import SecretResolutionError, resolve
    from jarn.doctor.extensions import collect_extensions
    from jarn.providers import ModelFactory, ModelResolutionError

    gpath = paths.global_config_path()
    home = paths.global_home()
    diag["jarn_home"] = str(home)
    overridden = paths.jarn_home_overridden()
    diag["jarn_home_overridden"] = overridden
    if overridden:
        diag["jarn_home_warning"] = (
            f"JARN_HOME is overridden ({home}) — secrets and the trust store "
            "live here; only set JARN_HOME in environments you trust."
        )
    diag["global_config"] = str(gpath)
    diag["global_config_present"] = gpath.is_file()

    # Permissions on the global tier. jarn tightens this at every start, so a
    # finding here means the chmod could not be applied — a directory owned by
    # another user, or a filesystem with no POSIX modes. Reported rather than
    # fixed silently, because the contents (prompt history, transcripts, memory,
    # the trust store) are already exposed by the time doctor sees it.
    diag["jarn_home_mode"] = None
    if os.name != "nt":
        try:
            mode = stat.S_IMODE(home.stat().st_mode)
        except OSError:
            mode = None
        if mode is not None:
            # POSIX bits only — a filesystem ACL can still grant access and is
            # deliberately neither inspected nor stripped here (stripping could
            # destroy a protective deny entry). SECURITY.md says so too.
            diag["jarn_home_mode"] = f"{mode:04o}"
            if mode & 0o077:
                diag["jarn_home_mode_warning"] = (
                    f"{home} is mode {mode:04o} — other local users can read your "
                    "prompt history, session transcripts, memory and trust store. "
                    f"Expected {paths.GLOBAL_HOME_MODE:04o}; run "
                    f"`chmod 700 {home}` (jarn could not do it itself)."
                )

    if config is None:
        from jarn.config.loader import load_config

        root = paths.find_project_root() if project_root is None else project_root
        diag["project_root"] = str(root) if root else None

        if not gpath.is_file():
            diag["ok"] = False
            return 1

        from jarn.config.trust import is_project_trusted

        if project_trusted is None:
            project_trusted = is_project_trusted(root) if root is not None else True
        diag["project_trusted"] = project_trusted
        cfg = load_config(project_root=root, project_trusted=project_trusted)
    else:
        cfg = config
        root = project_root
        diag["project_root"] = str(root) if root else None
        diag["global_config_present"] = True
        if project_trusted is None:
            project_trusted = True
        diag["project_trusted"] = project_trusted

    # Active in-scope roots: the primary project root first, then any added
    # (--add-dir / /add-dir) roots that widen the WRITE scope for this session.
    active_roots: list[str] = [str(root)] if root else []
    active_roots.extend(str(p) for p in (extra_roots or []))
    diag["roots"] = active_roots

    diag["default_profile"] = cfg.default_profile
    diag["main_model"] = cfg.resolved_main_model()
    diag["permission_mode"] = cfg.permission_mode.value
    diag["web_tools"] = cfg.policy.web_tools
    from jarn.config.schema import PermissionMode

    diag["effective_mode"] = (
        PermissionMode.PLAN.value if not project_trusted else cfg.permission_mode.value
    )

    stripped: list[str] = []
    if not project_trusted and root is not None:
        from jarn.config.paths import project_config_path
        from jarn.config.trust import stripped_project_keys

        ppath = project_config_path(root)
        if ppath is not None and ppath.is_file():
            from jarn.config.loader import _read_yaml

            stripped = stripped_project_keys(_read_yaml(ppath) or {})
    diag["project_stripped_keys"] = stripped

    factory = ModelFactory(cfg)
    ok = True
    providers: list[dict] = []
    for name, prov in cfg.providers.items():
        entry: dict[str, Any] = {"name": name, "type": prov.type.value}
        from jarn.config.schema import ProviderType

        if prov.type is ProviderType.CODEX_SUBSCRIPTION:
            from jarn.providers.codex_subscription import (
                CodexSubscriptionError,
                codex_subscription_account,
            )

            entry["key_source"] = "codex-managed"
            try:
                account = codex_subscription_account(cwd=root, timeout_seconds=10, refresh=False)
                connected = (account or {}).get("type") == "chatgpt"
                plan = (account or {}).get("planType")
                entry["key_state"] = (
                    f"ChatGPT subscription ({plan or 'unknown plan'})"
                    if connected
                    else "not signed in with ChatGPT — run `jarn codex login`"
                )
                entry["key_ok"] = connected
                entry["plan_type"] = plan
            except CodexSubscriptionError as exc:
                entry["key_state"] = str(exc)
                entry["key_ok"] = False
            if not entry["key_ok"] and name == cfg.default_profile:
                ok = False
            providers.append(entry)
            continue
        # Show the key source (env / keychain / file) — never the raw value.
        from jarn.onboarding.oauth import key_source as _key_source

        entry["key_source"] = _key_source(prov.api_key)
        try:
            resolve(prov.api_key)
            entry["key_state"] = "key ok"
            entry["key_ok"] = True
        except SecretResolutionError as exc:
            entry["key_state"] = str(exc)
            entry["key_ok"] = False
            if name == cfg.default_profile:
                ok = False
        providers.append(entry)
    diag["providers"] = providers

    try:
        factory.build_main()
        diag["main_model_builds"] = True
        diag["main_model_error"] = None
    except ModelResolutionError as exc:
        diag["main_model_builds"] = False
        diag["main_model_error"] = str(exc)
        ok = False

    from jarn.agent.docker_backend import docker_available as _docker_available
    from jarn.agent.os_sandbox import available as _sbx_available
    from jarn.agent.os_sandbox import backend_name as _sbx_name

    diag["sandbox"] = {
        "backend": _sbx_name(),
        "available": _sbx_available(),
        "mode": cfg.execution.local_sandbox,
    }
    diag["execution"] = {
        "backend": cfg.execution.backend,
        "docker_image": cfg.execution.docker_image,
        "docker_available": _docker_available(),
    }

    diag["git"] = {
        "autocheckpoint": cfg.git.autocheckpoint,
    }
    diag["wiki"] = {
        "enabled": cfg.wiki.enabled,
    }
    diag["observability"] = {
        "transcript": cfg.observability.transcript,
    }
    diag["context"] = {
        "repo_map": cfg.context.repo_map,
        "repo_map_tokens": cfg.context.repo_map_tokens,
        "memory_tokens": cfg.context.memory_tokens,
        "wiki_index_tokens": cfg.context.wiki_index_tokens,
        "project_context_tokens": cfg.context.project_context_tokens,
        "skill_catalog_tokens": cfg.context.skill_catalog_tokens,
    }

    if prompt_modules is None:
        try:
            from jarn.agent.prompt_modules import (
                PromptModuleContext,
                create_prompt_module_registry,
                prompt_module_diagnostics,
                with_context_budgets,
            )
            from jarn.extensibility.skills import load_skills

            skills = load_skills(
                root,
                project_trusted=bool(project_trusted),
                read_claude_dir=cfg.compat.read_claude_dir,
            )
            module_context = PromptModuleContext(
                config=cfg,
                project_root=root,
                project_trusted=bool(project_trusted),
                skills=skills,
            )
            module_registry = with_context_budgets(
                create_prompt_module_registry(skills), module_context
            )
            module_assembly = module_registry.assemble(module_context)
            prompt_modules = prompt_module_diagnostics(
                module_registry, module_context, module_assembly
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must remain usable
            prompt_modules = {"prompt_tokens": None, "modules": [], "error": str(exc)}
    diag["prompt_modules"] = prompt_modules

    diag["extensions"] = collect_extensions(root, project_trusted=project_trusted, config=cfg)

    # Search provider diagnostics
    from jarn.agent.web_tools import _resolve_provider_key

    search_provider = cfg.search.provider.value
    if search_provider in ("tavily", "brave", "exa"):
        key_resolved = _resolve_provider_key(search_provider, cfg) is not None
    elif search_provider == "auto":
        key_resolved = any(
            _resolve_provider_key(p, cfg) is not None for p in ("tavily", "brave", "exa")
        )
    else:  # duckduckgo
        key_resolved = True  # keyless
    diag["search"] = {
        "provider": search_provider,
        "key_resolved": key_resolved,
    }

    # Telegram gateway extra: warn when gateway: is enabled but aiogram missing.
    # Soft-fails when the gateway schema is not yet on Config (reads raw YAML).
    from jarn.doctor.telegram_extra import (
        aiogram_installed,
        gateway_enabled,
        telegram_extra_warnings,
    )

    gw_enabled = gateway_enabled(cfg, global_config_path=gpath)
    gw_warnings = telegram_extra_warnings(cfg, global_config_path=gpath)
    diag["gateway"] = {
        "enabled": gw_enabled,
        "telegram_extra_installed": aiogram_installed() if gw_enabled else None,
        "warnings": gw_warnings,
    }
    if gw_warnings:
        diag.setdefault("warnings", []).extend(gw_warnings)

    diag["ok"] = ok
    return 0 if ok else 1
