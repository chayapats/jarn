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
    assert _provider(tmp_path).complete("/mode ") == []


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
