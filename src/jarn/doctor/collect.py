"""Doctor diagnostic data collection (no rendering)."""

from __future__ import annotations

from typing import Any


def collect_doctor(
    diag: dict,
    *,
    config: Any = None,
    project_root: Any = None,
    project_trusted: bool | None = None,
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
    }

    diag["extensions"] = collect_extensions(
        root, project_trusted=project_trusted, config=cfg
    )

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

    diag["ok"] = ok
    return 0 if ok else 1
