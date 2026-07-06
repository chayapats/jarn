"""Input completion provider tests."""

from __future__ import annotations

from pathlib import Path

from jarn.tui.completion import CompletionProvider


def _provider(tmp_path):
    return CompletionProvider(
        command_catalog={
            "help": "Show available commands",
            "model": "Show or switch model",
            "mode": "Show or switch permission mode",
            "memory": "Long-term memory",
        },
        project_root=tmp_path,
    )


def test_command_completion(tmp_path):
    cands = _provider(tmp_path).complete("/mo")
    labels = [c.label for c in cands]
    assert "/model" in labels and "/mode" in labels
    assert all(c.kind == "command" for c in cands)
    assert cands[0].replacement.endswith(" ")  # trailing space after command
    by_label = {c.label: c for c in cands}
    assert by_label["/model"].description == "Show or switch model"


def test_command_completion_empty_prefix_lists_all(tmp_path):
    assert len(_provider(tmp_path).complete("/")) == 4


def test_no_completion_after_command_space(tmp_path):
    cands = _provider(tmp_path).complete("/mode ")
    labels = [c.label for c in cands]
    assert "plan" in labels and "ask" in labels and "auto-edit" in labels and "yolo" in labels
    assert all(c.kind == "argument" for c in cands)


def test_model_arg(tmp_path):
    provider = CompletionProvider(
        command_catalog={"model": "Show or switch model"},
        project_root=tmp_path,
        model_refs=["anthropic/claude-3-5-sonnet", "openai/gpt-4o"],
    )
    cands = provider.complete("/model anth")
    labels = [c.label for c in cands]
    assert labels == ["anthropic/claude-3-5-sonnet"]
    assert cands[0].replacement == "/model anthropic/claude-3-5-sonnet"


def test_preset_arg(tmp_path):
    provider = CompletionProvider(
        command_catalog={"preset": "Apply a policy preset"},
        project_root=tmp_path,
        preset_names=["trusted-repo", "review-only", "ci"],
    )
    cands = provider.complete("/preset rev")
    assert [c.label for c in cands] == ["review-only"]
    assert cands[0].replacement == "/preset review-only"


def test_completer_wires_controller_refs(tmp_path, base_config):
    """REPL CompletionProvider receives model/session/MCP lists from the controller."""
    from jarn.config.schema import MCPServer
    from jarn.repl.app import InlineApp

    base_config.mcp_servers = [
        MCPServer(name="fs", transport="stdio", command="echo"),
    ]
    app = InlineApp(base_config, tmp_path)
    app.controller.sessions.touch("thread-abc123", "my session", when=1.0)
    provider = app._completer()

    assert any("openrouter" in ref for ref in (provider.model_refs or []))
    assert "thread-abc123" in (provider.session_titles or [])
    assert "fs" in (provider.mcp_servers or [])
    app.controller.close()


def test_file_completion(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    cands = _provider(tmp_path).complete("look at @READ")
    assert any(c.label == "@README.md" for c in cands)
    chosen = next(c for c in cands if c.label == "@README.md")
    assert chosen.replacement == "look at @README.md"


def test_file_completion_directory_suffix(tmp_path):
    (tmp_path / "src").mkdir()
    cands = _provider(tmp_path).complete("@sr")
    assert any(c.label == "@src/" for c in cands)


def test_file_completion_into_subdir(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    cands = _provider(tmp_path).complete("@src/a")
    assert any(c.label == "@src/app.py" for c in cands)


def test_hidden_files_excluded_unless_dotted(tmp_path):
    (tmp_path / ".secret").write_text("x", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
    assert not any(".secret" in c.label for c in _provider(tmp_path).complete("@"))
    assert any(".secret" in c.label for c in _provider(tmp_path).complete("@.se"))


def test_plain_text_no_completion(tmp_path):
    assert _provider(tmp_path).complete("just chatting") == []


def test_completion_catalog_includes_builtins():
    from jarn.extensibility.commands import BUILTINS, completion_catalog

    catalog = completion_catalog()
    assert len(catalog) == len(BUILTINS)
    assert catalog["help"] == next(c.description for c in BUILTINS if c.name == "help")


# ---------------------------------------------------------------------------
# Rich @-mentions: @folder: and @symbol: (first slice)
# ---------------------------------------------------------------------------


def test_folder_mention_lists_dirs_only(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    cands = _provider(tmp_path).complete("@folder:sr")
    labels = [c.label for c in cands]
    assert "@src/" in labels
    assert not any("README" in label for label in labels)
    assert all(c.kind == "folder" for c in cands)


def test_folder_mention_replacement(tmp_path):
    (tmp_path / "src").mkdir()
    cands = _provider(tmp_path).complete("look at @folder:sr")
    chosen = next(c for c in cands if c.label == "@src/")
    assert chosen.replacement == "look at @src/"


def test_symbol_mention_matches_class_and_func(tmp_path):
    src = "class Foo:\n    def bar(self):\n        pass\n\n\ndef top():\n    pass\n"
    (tmp_path / "mod.py").write_text(src, encoding="utf-8")
    import jarn.agent.repomap as repomap_mod

    repomap_mod._SYMBOL_INDEX_CACHE.clear()

    cands = _provider(tmp_path).complete("@symbol:Fo")
    foo = next(c for c in cands if "Foo" in c.label)
    assert foo.kind == "symbol"
    assert foo.replacement == "@mod.py:Foo"
    assert foo.description == "mod.py"

    method_cands = _provider(tmp_path).complete("@symbol:ba")
    bar = next(c for c in method_cands if "bar" in c.label)
    assert "Foo" in bar.label  # container shown in the menu label
    assert bar.replacement == "@mod.py:bar"
    assert bar.description == "mod.py"


def test_symbol_mention_case_insensitive_and_capped(tmp_path):
    funcs = "\n\n".join(f"def sym_{i}():\n    pass" for i in range(50))
    (tmp_path / "many.py").write_text(funcs, encoding="utf-8")
    import jarn.agent.repomap as repomap_mod

    repomap_mod._SYMBOL_INDEX_CACHE.clear()

    cands = _provider(tmp_path).complete("@symbol:SYM")  # uppercase -> matches sym_*
    assert len(cands) > 0
    assert len(cands) <= 12


def test_symbol_mention_empty_fragment_returns_nothing(tmp_path):
    """@symbol: with no fragment yields [] — don't dump every symbol arbitrarily."""
    (tmp_path / "mod.py").write_text("def alpha(): pass\ndef beta(): pass\n", encoding="utf-8")
    import jarn.agent.repomap as repomap_mod

    repomap_mod._SYMBOL_INDEX_CACHE.clear()
    repomap_mod._DISCOVERY_CACHE.clear()
    assert _provider(tmp_path).complete("@symbol:") == []


def test_symbol_mention_sorted_before_cap(tmp_path):
    """Matches come back in deterministic (name) order, not arbitrary git-ls-files
    order, so the max_files cap doesn't hide symbols unpredictably."""
    for f in ("z.py", "a.py", "m.py"):
        (tmp_path / f).write_text(
            "\n\n".join(f"def handler_{f[0]}_{i}(): pass" for i in range(3)),
            encoding="utf-8",
        )
    import jarn.agent.repomap as repomap_mod

    repomap_mod._SYMBOL_INDEX_CACHE.clear()
    repomap_mod._DISCOVERY_CACHE.clear()
    cands = _provider(tmp_path).complete("@symbol:handler")
    names = [c.replacement.split(":")[-1] for c in cands]
    assert names == sorted(names)  # deterministic alphabetical, not git order


def test_bare_at_still_files_unchanged(tmp_path):
    """Regression guard: bare @ stays file completion, byte-for-byte."""
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    cands = _provider(tmp_path).complete("@READ")
    assert any(c.label == "@README.md" for c in cands)
    assert all(c.kind == "file" for c in cands)


def test_unknown_kind_prefix_no_crash(tmp_path):
    """An unknown @kind: token must not raise (falls through to file resolver)."""
    assert _provider(tmp_path).complete("@bogus:x") == []


def test_consecutive_keystrokes_reuse_cached_listing(tmp_path, monkeypatch):
    """Typing successive characters in the same directory must not re-scan it."""
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "alphabet.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta.py").write_text("x", encoding="utf-8")
    provider = _provider(tmp_path)

    import jarn.tui.completion as completion_mod

    real_iterdir = Path.iterdir
    calls = {"n": 0}

    def counting_iterdir(self):
        calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(completion_mod.Path, "iterdir", counting_iterdir)

    # Three keystrokes building "@alpha" in the same directory.
    provider.complete("@a")
    provider.complete("@al")
    provider.complete("@alp")

    assert calls["n"] == 1  # only the first keystroke scanned the directory


def test_cached_listing_results_unchanged(tmp_path):
    """The cache must not alter the returned candidates."""
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "alphabet.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta.py").write_text("x", encoding="utf-8")
    provider = _provider(tmp_path)

    first = provider.complete("@al")
    second = provider.complete("@al")
    labels = [c.label for c in second]
    assert first == second
    assert "@alpha.py" in labels and "@alphabet.py" in labels
    assert "@beta.py" not in labels


def test_cache_isolated_per_directory(tmp_path):
    """Different directories keep their own cached listings."""
    (tmp_path / "root.txt").write_text("x", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.py").write_text("x", encoding="utf-8")
    provider = _provider(tmp_path)

    assert any(c.label == "@root.txt" for c in provider.complete("@ro"))
    assert any(c.label == "@sub/inner.py" for c in provider.complete("@sub/in"))
    # Back to the root directory still works.
    assert any(c.label == "@root.txt" for c in provider.complete("@ro"))


def test_cache_refreshes_when_directory_changes(tmp_path):
    """A newly created file appears once the directory mtime changes."""
    (tmp_path / "one.txt").write_text("x", encoding="utf-8")
    provider = _provider(tmp_path)
    provider.complete("@")  # prime cache

    import os

    new = tmp_path / "two.txt"
    new.write_text("x", encoding="utf-8")
    # Force the directory mtime forward so the cache is considered stale.
    future = new.stat().st_mtime + 10
    os.utime(tmp_path, (future, future))

    labels = [c.label for c in provider.complete("@")]
    assert "@two.txt" in labels


# ---------------------------------------------------------------------------
# T-2-5: Fuzzy completion tier
# ---------------------------------------------------------------------------


def test_fuzzy_command(tmp_path):
    """/cmit → /commit via fuzzy subsequence matching (c-m-i-t in order)."""
    provider = CompletionProvider(
        command_catalog={
            "commit": "Commit changes",
            "comment": "Add a comment",
            "clear": "Clear screen",
        },
        project_root=tmp_path,
    )
    labels = [c.label for c in provider.complete("/cmit")]
    assert "/commit" in labels


def test_fuzzy_file(tmp_path):
    """@pyproj → pyproject.toml via fuzzy subsequence matching."""
    (tmp_path / "pyproject.toml").write_text("x", encoding="utf-8")
    (tmp_path / "setup.cfg").write_text("x", encoding="utf-8")
    provider = CompletionProvider(command_catalog={}, project_root=tmp_path)
    labels = [c.label for c in provider.complete("@pyprjct")]
    assert "@pyproject.toml" in labels


def test_prefix_ranks_first(tmp_path):
    """Prefix matches (tier 1) always precede fuzzy-only matches (tier 2)."""
    provider = CompletionProvider(
        command_catalog={
            "model": "Show or switch model",
            "mode": "Show or switch mode",
            "memory": "Long-term memory",  # 'mo' is a subsequence but NOT a prefix
        },
        project_root=tmp_path,
    )
    cands = provider.complete("/mo")
    labels = [c.label for c in cands]
    # tier 1: mode and model are prefix matches → must be present
    assert "/model" in labels and "/mode" in labels
    # tier 2: 'mo' is a subsequence of 'memory' (m..o) → also present after fuzzy
    assert "/memory" in labels
    # tier 1 entries must precede tier 2 entries
    assert labels.index("/memory") > max(labels.index("/model"), labels.index("/mode"))


def test_no_match_empty(tmp_path):
    """A query with no subsequence match returns nothing (empty list)."""
    provider = CompletionProvider(
        command_catalog={"help": "Show help"},
        project_root=tmp_path,
    )
    # 'xyz' contains no characters present in 'help' in order
    assert provider.complete("/xyz") == []


def test_fuzzy_rank_word_boundary_bonus():
    """Word-boundary matches score higher than mid-word matches."""
    from jarn.tui.completion import fuzzy_rank

    # "cm": "common" has 'c' at word boundary (pos 0) → higher score
    # "decimal" has 'c' mid-word (pos 2) → lower score
    result = fuzzy_rank("cm", ["decimal", "common"])
    assert result[0] == "common"


def test_fuzzy_rank_gap_penalty_ordering():
    """Fewer gaps → higher score → ranked first."""
    from jarn.tui.completion import fuzzy_rank

    # "ac" in "abcdef": a(0)→c(2), 1-char gap
    # "ac" in "axxxxxc": a(0)→c(6), 5-char gap
    result = fuzzy_rank("ac", ["axxxxxc", "abcdef"])
    assert result[0] == "abcdef"


# ---------------------------------------------------------------------------
# T-2-9: @git: and @url: mentions
# ---------------------------------------------------------------------------


def test_git_mention_completion(tmp_path):
    """``@git:`` completes all 4 read-only subcommands."""
    cands = _provider(tmp_path).complete("@git:")
    labels = [c.label for c in cands]
    assert "@git:status" in labels
    assert "@git:diff" in labels
    assert "@git:staged" in labels
    assert "@git:log" in labels
    assert all(c.kind == "git" for c in cands)


def test_git_mention_completion_prefix_filter(tmp_path):
    """``@git:st`` should complete to ``@git:status`` only (prefix filter)."""
    cands = _provider(tmp_path).complete("@git:st")
    labels = [c.label for c in cands]
    assert "@git:status" in labels
    assert "@git:diff" not in labels


def test_git_mention_completion_replacement(tmp_path):
    """Chosen completion replaces the mention token while preserving the prefix."""
    cands = _provider(tmp_path).complete("look at @git:")
    status = next(c for c in cands if c.label == "@git:status")
    assert status.replacement == "look at @git:status"
