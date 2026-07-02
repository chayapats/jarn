"""Async/remote subagent config + wiring tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.agent.builder import _async_subagent_specs, build_runtime
from jarn.config.loader import ConfigError, load_config


def _cfg_with_async(tmp_path, entry):
    gp = tmp_path / "g.yaml"
    gp.write_text(yaml.safe_dump({"async_subagents": [entry]}), encoding="utf-8")
    return load_config(global_path=gp, project_path=None)


def test_async_subagent_parsed(tmp_path):
    cfg = _cfg_with_async(tmp_path, {
        "name": "researcher", "description": "deep research",
        "graph_id": "research", "url": "https://x/agent",
    })
    assert cfg.async_subagents[0].name == "researcher"
    assert cfg.async_subagents[0].graph_id == "research"


def test_async_subagent_missing_field_raises(tmp_path):
    with pytest.raises(ConfigError):
        _cfg_with_async(tmp_path, {"name": "x", "description": "y"})  # no graph_id


def test_spec_building(base_config):
    from jarn.config.schema import AsyncSubagentSpec

    base_config.async_subagents = [
        AsyncSubagentSpec(name="r", description="d", graph_id="g",
                          url="https://x", headers={"A": "b"})
    ]
    specs = _async_subagent_specs(base_config)
    assert specs == [{"name": "r", "description": "d", "graph_id": "g",
                      "url": "https://x", "headers": {"A": "b"}}]


def test_build_runtime_with_async_subagent(base_config, tmp_path):
    from jarn.config.schema import AsyncSubagentSpec

    base_config.async_subagents = [
        AsyncSubagentSpec(name="r", description="d", graph_id="g")
    ]
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = build_runtime(base_config, project_root=tmp_path)
    assert type(rt.agent).__name__ == "CompiledStateGraph"


def _find_hitl(obj, _depth=0, _seen=None):
    """Walk an object's closure/attrs to the HumanInTheLoopMiddleware it wraps."""
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    if _seen is None:
        _seen = set()
    if id(obj) in _seen or _depth > 6:
        return None
    _seen.add(id(obj))
    if isinstance(obj, HumanInTheLoopMiddleware):
        return obj
    closure = getattr(obj, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                contents = cell.cell_contents
            except ValueError:
                continue
            found = _find_hitl(contents, _depth + 1, _seen)
            if found is not None:
                return found
    for attr in ("func", "bound", "__self__", "__wrapped__"):
        value = getattr(obj, attr, None)
        if value is not None:
            found = _find_hitl(value, _depth + 1, _seen)
            if found is not None:
                return found
    return None


def _compiled_interrupt_keys(agent) -> set[str]:
    """Pull the ``interrupt_on`` keys off *this* compiled agent's installed
    HumanInTheLoopMiddleware — the actual gate that fires on tool calls.

    This is the load-bearing check: it confirms the async tool names land in the
    real interrupt config of the *compiled* graph (not just in our dict), so the
    middleware-injected async tools are genuinely gated rather than bypassing the
    permission engine. The middleware is reached through the specific HITL node
    of this agent (not a global object scan), so it stays accurate even after a
    prior test built an agent that did gate the async tools.
    """
    node = agent.nodes.get("HumanInTheLoopMiddleware.after_model")
    assert node is not None, "HumanInTheLoopMiddleware not installed"
    hitl = _find_hitl(node.bound)
    assert hitl is not None, "could not locate HITL middleware on the node"
    return set(hitl.interrupt_on.keys())


def test_compiled_agent_gates_async_tools(base_config, tmp_path):
    """With async subagents configured, the compiled agent's HITL middleware
    gates all five async-subagent tool names (verified, not assumed)."""
    from jarn.agent.permissions_bridge import ASYNC_SUBAGENT_TOOLS
    from jarn.config.schema import AsyncSubagentSpec

    base_config.async_subagents = [
        AsyncSubagentSpec(name="r", description="d", graph_id="g")
    ]
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = build_runtime(base_config, project_root=tmp_path)
    keys = _compiled_interrupt_keys(rt.agent)
    assert set(ASYNC_SUBAGENT_TOOLS) <= keys


def _build_with_async(base_config, tmp_path, *specs):
    base_config.async_subagents = list(specs)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        return build_runtime(base_config, project_root=tmp_path)


def test_ambient_key_blocks_nonlocal_url(base_config, tmp_path, monkeypatch):
    """A non-local async-subagent url plus an ambient LangGraph key fails closed."""
    from jarn.agent.builder import AmbientKeyLeakError
    from jarn.config.schema import AsyncSubagentSpec

    for var in ("LANGGRAPH_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "secret-ambient")

    with pytest.raises(AmbientKeyLeakError) as exc:
        _build_with_async(
            base_config, tmp_path,
            AsyncSubagentSpec(name="r", description="d", graph_id="g",
                              url="https://evil.example.com/agent"),
        )
    assert "https://evil.example.com/agent" in exc.value.messages[0]
    assert "LANGSMITH_API_KEY" in exc.value.messages[0]


def test_no_ambient_key_allows_nonlocal_url(base_config, tmp_path, monkeypatch):
    """Same non-local url but no ambient key in the env: nothing to leak."""
    from jarn.config.schema import AsyncSubagentSpec

    for var in ("LANGGRAPH_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    rt = _build_with_async(
        base_config, tmp_path,
        AsyncSubagentSpec(name="r", description="d", graph_id="g",
                          url="https://evil.example.com/agent"),
    )
    assert rt.agent is not None


def test_local_url_allows_build_even_with_ambient_key(
    base_config, tmp_path, monkeypatch
):
    """A localhost url is the operator's own machine — ambient key is not exfiltration."""
    from jarn.config.schema import AsyncSubagentSpec

    monkeypatch.setenv("LANGGRAPH_API_KEY", "secret-ambient")

    rt = _build_with_async(
        base_config, tmp_path,
        AsyncSubagentSpec(name="r", description="d", graph_id="g",
                          url="http://localhost:2024"),
        AsyncSubagentSpec(name="r2", description="d", graph_id="g",
                          url="http://127.0.0.1:8000/agent"),
    )
    assert rt.agent is not None


def test_untrusted_project_does_not_gate_async_tools(tmp_path):
    """An untrusted-loaded project has its async_subagents stripped, so no async
    tools are configured and none are gated (no phantom interrupts)."""
    from jarn.agent.permissions_bridge import ASYNC_SUBAGENT_TOOLS

    pp = tmp_path / "p.yaml"
    pp.write_text(
        yaml.safe_dump({"async_subagents": [
            {"name": "x", "description": "d", "graph_id": "g"}
        ]}),
        encoding="utf-8",
    )
    cfg = load_config(global_path=None, project_path=pp, project_trusted=False)
    assert cfg.async_subagents == []

    # Provide a model so build_runtime doesn't depend on the dev's real global
    # config (the test home is isolated); the build itself is mocked.
    cfg.default_model = "openrouter/test-model"
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = build_runtime(cfg, project_root=tmp_path)
    keys = _compiled_interrupt_keys(rt.agent)
    assert not (set(ASYNC_SUBAGENT_TOOLS) & keys)
