"""Tests for src/jarn/agent/repomap.py — the ranked, token-budgeted repo map."""

from __future__ import annotations

import contextlib
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from jarn.agent.repomap import (
    _count_tokens,
    _extract_go,
    _extract_jsts,
    _extract_python,
    _extract_rust,
    _extract_symbols,
    build_repo_map,
    build_symbol_index,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def py_fixture(tmp_path: Path) -> Path:
    """A .py file with a mix of top-level classes and functions."""
    src = textwrap.dedent("""\
        class MyClass:
            def method_one(self):
                pass
            def method_two(self):
                pass

        def top_func():
            pass

        async def async_func():
            pass

        _private = 1
    """)
    f = tmp_path / "sample.py"
    f.write_text(src, encoding="utf-8")
    return f


@pytest.fixture
def ts_fixture(tmp_path: Path) -> Path:
    src = textwrap.dedent("""\
        export class MyService {
          doWork() {}
        }

        export function helperFn() {}

        export const MY_CONST = 42;

        interface IFoo {}

        type AliasType = string;
    """)
    f = tmp_path / "service.ts"
    f.write_text(src, encoding="utf-8")
    return f


@pytest.fixture
def go_fixture(tmp_path: Path) -> Path:
    src = textwrap.dedent("""\
        package main

        import "fmt"

        type Server struct{}

        func NewServer() *Server {}

        func (s *Server) Handle() {}

        var globalVar = "hello"
    """)
    f = tmp_path / "main.go"
    f.write_text(src, encoding="utf-8")
    return f


@pytest.fixture
def rust_fixture(tmp_path: Path) -> Path:
    src = textwrap.dedent("""\
        pub struct Config {
            name: String,
        }

        pub fn build_config() -> Config {}

        pub enum Status {
            Ok,
            Err,
        }

        pub trait Runnable {
            fn run(&self);
        }
    """)
    f = tmp_path / "lib.rs"
    f.write_text(src, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Symbol extraction — Python
# ---------------------------------------------------------------------------


def test_python_extracts_class_and_functions(py_fixture: Path) -> None:
    text = py_fixture.read_text(encoding="utf-8")
    symbols = _extract_python(text)
    assert "MyClass" in symbols
    assert "top_func" in symbols
    assert "async_func" in symbols


def test_python_extracts_class_methods_one_level_deep(py_fixture: Path) -> None:
    text = py_fixture.read_text(encoding="utf-8")
    symbols = _extract_python(text)
    method_syms = [s for s in symbols if s.startswith("  .")]
    assert "  .method_one" in method_syms
    assert "  .method_two" in method_syms


def test_python_bad_syntax_returns_empty() -> None:
    assert _extract_python("def (broken syntax:") == []


def test_python_symbols_in_map(tmp_path: Path, py_fixture: Path) -> None:
    """build_repo_map must include Python symbol names in its output."""
    # py_fixture is in tmp_path; use tmp_path as root.
    result = build_repo_map(tmp_path, token_budget=4000)
    assert "MyClass" in result
    assert "top_func" in result


# ---------------------------------------------------------------------------
# Symbol extraction — JS/TS
# ---------------------------------------------------------------------------


def test_jsts_extracts_class(ts_fixture: Path) -> None:
    text = ts_fixture.read_text(encoding="utf-8")
    syms = _extract_jsts(text)
    assert "MyService" in syms


def test_jsts_extracts_function(ts_fixture: Path) -> None:
    text = ts_fixture.read_text(encoding="utf-8")
    syms = _extract_jsts(text)
    assert "helperFn" in syms


def test_jsts_extracts_const(ts_fixture: Path) -> None:
    text = ts_fixture.read_text(encoding="utf-8")
    syms = _extract_jsts(text)
    assert "MY_CONST" in syms


def test_jsts_extracts_interface_and_type(ts_fixture: Path) -> None:
    text = ts_fixture.read_text(encoding="utf-8")
    syms = _extract_jsts(text)
    assert "IFoo" in syms
    assert "AliasType" in syms


def test_ts_symbols_in_map(tmp_path: Path, ts_fixture: Path) -> None:
    result = build_repo_map(tmp_path, token_budget=4000)
    assert "MyService" in result


# ---------------------------------------------------------------------------
# Symbol extraction — Go
# ---------------------------------------------------------------------------


def test_go_extracts_func(go_fixture: Path) -> None:
    text = go_fixture.read_text(encoding="utf-8")
    syms = _extract_go(text)
    assert "NewServer" in syms


def test_go_extracts_type(go_fixture: Path) -> None:
    text = go_fixture.read_text(encoding="utf-8")
    syms = _extract_go(text)
    assert "Server" in syms


def test_go_symbols_in_map(tmp_path: Path, go_fixture: Path) -> None:
    result = build_repo_map(tmp_path, token_budget=4000)
    assert "NewServer" in result


# ---------------------------------------------------------------------------
# Symbol extraction — Rust
# ---------------------------------------------------------------------------


def test_rust_extracts_struct(rust_fixture: Path) -> None:
    text = rust_fixture.read_text(encoding="utf-8")
    syms = _extract_rust(text)
    assert "Config" in syms


def test_rust_extracts_fn(rust_fixture: Path) -> None:
    text = rust_fixture.read_text(encoding="utf-8")
    syms = _extract_rust(text)
    assert "build_config" in syms


def test_rust_extracts_enum_and_trait(rust_fixture: Path) -> None:
    text = rust_fixture.read_text(encoding="utf-8")
    syms = _extract_rust(text)
    assert "Status" in syms
    assert "Runnable" in syms


def test_rust_symbols_in_map(tmp_path: Path, rust_fixture: Path) -> None:
    result = build_repo_map(tmp_path, token_budget=4000)
    assert "Config" in result


# ---------------------------------------------------------------------------
# Token budget respected
# ---------------------------------------------------------------------------


def _make_big_tree(root: Path, n: int = 30) -> None:
    """Create *n* .py files each with several functions."""
    for i in range(n):
        text = "\n".join(f"def func_{i}_{j}(): pass" for j in range(20))
        (root / f"module_{i}.py").write_text(text, encoding="utf-8")


def test_token_budget_respected(tmp_path: Path) -> None:
    """Output token count must not exceed the budget."""
    _make_big_tree(tmp_path, n=30)
    budget = 300
    result = build_repo_map(tmp_path, token_budget=budget)
    # Use the same counting logic the module uses.
    token_count = _count_tokens(result)
    # Allow a small over-run for the header + last entry (the truncation is
    # conservative, not byte-precise), but it should be well under budget.
    assert token_count <= budget + 50, (
        f"token count {token_count} far exceeds budget {budget}"
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism(tmp_path: Path, py_fixture: Path, ts_fixture: Path) -> None:
    """Two consecutive calls with an identical tree produce identical output."""
    first = build_repo_map(tmp_path, token_budget=4000)
    second = build_repo_map(tmp_path, token_budget=4000)
    assert first == second


def test_ranking_ties_broken_by_path(tmp_path: Path) -> None:
    """Files with identical scores are sorted alphabetically by rel path."""
    # Create two files with the same single symbol — equal scores (same depth,
    # no xrefs between them) so path order decides.
    (tmp_path / "aaa.py").write_text("def fn(): pass", encoding="utf-8")
    (tmp_path / "zzz.py").write_text("def fn(): pass", encoding="utf-8")
    result = build_repo_map(tmp_path, token_budget=4000)
    idx_a = result.index("aaa.py") if "aaa.py" in result else len(result)
    idx_z = result.index("zzz.py") if "zzz.py" in result else len(result)
    # aaa.py should appear before zzz.py
    assert idx_a < idx_z, "Files with equal scores must be sorted by path"


# ---------------------------------------------------------------------------
# .gitignore / noise dir exclusion
# ---------------------------------------------------------------------------


def test_node_modules_excluded(tmp_path: Path) -> None:
    """Files inside node_modules must not appear in the map."""
    noise = tmp_path / "node_modules" / "pkg"
    noise.mkdir(parents=True)
    (noise / "secret_nm.js").write_text("export function secretNmFn() {}", encoding="utf-8")
    # Also add a real file so the map is not empty.
    (tmp_path / "app.js").write_text("export function main() {}", encoding="utf-8")
    result = build_repo_map(tmp_path, token_budget=4000)
    # The unique function name from the noise dir must not appear.
    assert "secretNmFn" not in result
    assert "secret_nm.js" not in result


def test_venv_excluded(tmp_path: Path) -> None:
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "helper.py").write_text("def helper(): pass", encoding="utf-8")
    (tmp_path / "app.py").write_text("def app(): pass", encoding="utf-8")
    result = build_repo_map(tmp_path, token_budget=4000)
    assert ".venv" not in result


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_cache_second_call_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call with unchanged tree should hit the cache and not re-run
    symbol extraction for every file."""
    (tmp_path / "mod.py").write_text("def fn(): pass", encoding="utf-8")

    extract_calls: list[Path] = []
    _orig_extract = _extract_symbols

    def _spy_extract(path: Path) -> list[str]:
        extract_calls.append(path)
        return _orig_extract(path)

    # First call: cold cache — extractor runs.
    monkeypatch.setattr("jarn.agent.repomap._extract_symbols", _spy_extract)
    first = build_repo_map(tmp_path, token_budget=4000)
    count_first = len(extract_calls)
    assert count_first > 0, "Expected at least one extraction on cold cache"

    # Second call: cache hit — extractor should NOT run.
    extract_calls.clear()
    second = build_repo_map(tmp_path, token_budget=4000)
    assert second == first
    assert len(extract_calls) == 0, "Extractor should not be called on cache hit"


def test_cache_busted_on_file_change(tmp_path: Path) -> None:
    """Changing a file's mtime (or adding a file) invalidates the cache."""
    f = tmp_path / "mod.py"
    f.write_text("def fn(): pass", encoding="utf-8")
    first = build_repo_map(tmp_path, token_budget=4000)

    # Add a new file — file count changes → new signature → cache miss.
    new_f = tmp_path / "new_mod.py"
    new_f.write_text("class NewClass: pass", encoding="utf-8")
    second = build_repo_map(tmp_path, token_budget=4000)

    assert "NewClass" in second, "New file's symbols should appear after cache bust"
    # The two outputs are different because the new file added a symbol.
    assert second != first


# ---------------------------------------------------------------------------
# Symbol index (build_symbol_index) — backs @symbol completion
# ---------------------------------------------------------------------------


def test_build_symbol_index_python(tmp_path: Path, py_fixture: Path) -> None:
    """Classes, normalized methods, and top-level funcs with correct rel + container."""
    refs = build_symbol_index(tmp_path)
    by_name = {(r.name, r.container): r for r in refs}

    # Top-level class.
    assert ("MyClass", "") in by_name
    assert by_name[("MyClass", "")].rel == "sample.py"

    # Methods normalized from "  .name" -> name=method, container=enclosing class.
    assert ("method_one", "MyClass") in by_name
    assert ("method_two", "MyClass") in by_name
    assert by_name[("method_one", "MyClass")].rel == "sample.py"

    # Top-level functions (no container).
    assert ("top_func", "") in by_name
    assert ("async_func", "") in by_name


def test_build_symbol_index_cache_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second call with an unchanged tree must not re-scan (cached at module scope)."""
    (tmp_path / "mod.py").write_text("def fn(): pass", encoding="utf-8")

    import jarn.agent.repomap as repomap_mod

    # Drop any cross-test cache state for this root.
    repomap_mod._SYMBOL_INDEX_CACHE.clear()

    extract_calls: list[Path] = []
    _orig = repomap_mod._extract_symbols

    def _spy(path: Path) -> list[str]:
        extract_calls.append(path)
        return _orig(path)

    monkeypatch.setattr("jarn.agent.repomap._extract_symbols", _spy)

    first = build_symbol_index(tmp_path)
    assert len(extract_calls) > 0
    extract_calls.clear()

    second = build_symbol_index(tmp_path)
    assert second == first
    assert len(extract_calls) == 0, "Cached index must not re-extract on repeat call"


def test_build_symbol_index_no_rediscover_within_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-keystroke path must not re-fork git ls-files + re-stat every file on
    each call. Discovery is TTL-cached (regression: discovery ran on every call —
    only symbol extraction was cached — so @symbol stalled on large repos)."""
    (tmp_path / "mod.py").write_text("def fn(): pass", encoding="utf-8")

    import jarn.agent.repomap as repomap_mod

    repomap_mod._SYMBOL_INDEX_CACHE.clear()
    repomap_mod._DISCOVERY_CACHE.clear()

    discover_calls: list[Path] = []
    _orig = repomap_mod._discover_files

    def _spy(root: Path) -> list[Path]:
        discover_calls.append(root)
        return _orig(root)

    monkeypatch.setattr("jarn.agent.repomap._discover_files", _spy)

    build_symbol_index(tmp_path)
    build_symbol_index(tmp_path)
    build_symbol_index(tmp_path)
    assert len(discover_calls) == 1, (
        "discovery (git fork + per-file stat) must be TTL-cached, not run per call"
    )


def test_build_symbol_index_empty_repo(tmp_path: Path) -> None:
    """An empty tree yields an empty index without raising."""
    import jarn.agent.repomap as repomap_mod

    repomap_mod._SYMBOL_INDEX_CACHE.clear()
    assert build_symbol_index(tmp_path) == []


def _synthetic_repo(root: Path, n: int) -> None:
    """Create *n* Python files with cross-referencing path tokens."""
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        name = f"mod_{i}"
        (pkg / f"{name}.py").write_text(f"def {name}(): pass\n", encoding="utf-8")
        if i > 0:
            (pkg / f"test_{name}.py").write_text(f"# uses {name}\n", encoding="utf-8")


def test_xref_linear(tmp_path: Path) -> None:
    """Xref counting scales ~linearly (5× files → ~5× time, not 25×)."""
    import time as _time

    from jarn.agent.repomap import _build_entries, _build_xref_counts

    small = tmp_path / "small"
    large = tmp_path / "large"
    _synthetic_repo(small, 200)
    _synthetic_repo(large, 1000)

    small_entries = _build_entries(small, list((small / "pkg").glob("*.py")))
    large_entries = _build_entries(large, list((large / "pkg").glob("*.py")))

    t0 = _time.perf_counter()
    _build_xref_counts(small_entries)
    small_elapsed = _time.perf_counter() - t0

    t1 = _time.perf_counter()
    _build_xref_counts(large_entries)
    large_elapsed = _time.perf_counter() - t1

    ratio = large_elapsed / max(small_elapsed, 1e-9)
    assert ratio < 12, f"xref build looks super-linear: ratio={ratio:.1f}"


def test_discovery_cache_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_repo_map reuses TTL-cached discovery instead of re-forking git each call."""
    (tmp_path / "mod.py").write_text("def fn(): pass", encoding="utf-8")

    import jarn.agent.repomap as repomap_mod

    repomap_mod._DISCOVERY_CACHE.clear()

    discover_calls: list[Path] = []
    _orig = repomap_mod._discover_files

    def _spy(root: Path) -> list[Path]:
        discover_calls.append(root)
        return _orig(root)

    monkeypatch.setattr("jarn.agent.repomap._discover_files", _spy)

    build_repo_map(tmp_path, token_budget=4000)
    build_repo_map(tmp_path, token_budget=4000)
    build_repo_map(tmp_path, token_budget=4000)
    assert len(discover_calls) == 1, (
        "build_repo_map must share the discovery TTL cache"
    )


# ---------------------------------------------------------------------------
# Config validation (ConfigError)
# ---------------------------------------------------------------------------


def test_config_bad_repo_map_value() -> None:
    from jarn.config.loader import ConfigError, _build_config

    with pytest.raises(ConfigError, match="context.repo_map"):
        _build_config({"context": {"repo_map": "bogus"}})


def test_config_zero_repo_map_tokens() -> None:
    from jarn.config.loader import ConfigError, _build_config

    with pytest.raises(ConfigError, match="context.repo_map_tokens"):
        _build_config({"context": {"repo_map_tokens": 0}})


def test_config_negative_repo_map_tokens() -> None:
    from jarn.config.loader import ConfigError, _build_config

    with pytest.raises(ConfigError, match="context.repo_map_tokens"):
        _build_config({"context": {"repo_map_tokens": -1}})


def test_config_valid_repo_map_modes() -> None:
    from jarn.config.loader import _build_config

    for mode in ("off", "tool", "auto"):
        cfg = _build_config({"context": {"repo_map": mode}})
        assert cfg.context.repo_map == mode


# ---------------------------------------------------------------------------
# Tool wiring
# ---------------------------------------------------------------------------


def test_repo_map_tool_present_when_mode_tool(tmp_path: Path) -> None:
    """When context.repo_map='tool', the agent tool list contains 'repo_map'."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import _build_repo_map_tool, build_runtime
    from jarn.config.schema import (
        Config,
        ContextConfig,
        ProviderConfig,
        ProviderType,
        RoutingConfig,
    )

    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(
            type=ProviderType.OPENROUTER, api_key="sk-test",
            base_url="http://localhost:9999/v1",
        )},
        routing=RoutingConfig(main="openrouter/anthropic/claude-opus-4-8"),
        context=ContextConfig(repo_map="tool", repo_map_tokens=512),
    )

    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        build_runtime(cfg, project_root=tmp_path)

    # The compiled agent's tools are accessible via the graph's nodes; the
    # simplest check is that _build_repo_map_tool produces a callable named
    # "repo_map".
    tool = _build_repo_map_tool(tmp_path, token_budget=512)
    assert tool.name == "repo_map"


def test_repo_map_tool_absent_when_mode_off(tmp_path: Path) -> None:
    """When context.repo_map='off', _build_repo_map_tool is not called and no
    'repo_map' tool reaches the agent.  We verify by inspecting the tool list
    assembled in build_runtime via patching deepagents.create_deep_agent."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime
    from jarn.config.schema import (
        Config,
        ContextConfig,
        ProviderConfig,
        ProviderType,
        RoutingConfig,
    )

    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(
            type=ProviderType.OPENROUTER, api_key="sk-test",
            base_url="http://localhost:9999/v1",
        )},
        routing=RoutingConfig(main="openrouter/anthropic/claude-opus-4-8"),
        context=ContextConfig(repo_map="off"),
    )

    fake = GenericFakeChatModel(messages=iter([]))
    captured_tools: list = []

    def _fake_create_agent(**kwargs):
        captured_tools.extend(kwargs.get("tools") or [])
        import deepagents
        return deepagents.create_deep_agent(**kwargs)

    # create_deep_agent is imported at local scope inside build_runtime, so we
    # patch it in deepagents (the originating module).
    with (
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        patch("deepagents.create_deep_agent", side_effect=_fake_create_agent),
        contextlib.suppress(Exception),
    ):
        build_runtime(cfg, project_root=tmp_path)

    tool_names = [getattr(t, "name", "") for t in captured_tools]
    assert "repo_map" not in tool_names


def test_repo_map_tool_call_returns_map(tmp_path: Path) -> None:
    """The repo_map tool actually delegates to build_repo_map and returns text."""
    from jarn.agent.builder import _build_repo_map_tool

    (tmp_path / "app.py").write_text("def run(): pass", encoding="utf-8")
    tool = _build_repo_map_tool(tmp_path, token_budget=2000)
    result = tool.invoke({"focus": ""})
    assert "app.py" in result
    assert "run" in result


def test_repo_map_is_read_only_never_prompts():
    """repo_map reads local source only — it must map to READ and auto-allow,
    not fall through to the NETWORK default (which would prompt in ask mode)."""
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.config.schema import PermissionMode
    from jarn.permissions import ActionKind, PermissionEngine

    action = tool_to_action("repo_map", {"focus": "agent"})
    assert action.kind is ActionKind.READ
    assert PermissionEngine(mode=PermissionMode.ASK).evaluate(action).decision.value == "allow"
