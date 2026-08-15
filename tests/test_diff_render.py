"""Tests for word-level (intraline) diff emphasis — T-2-8."""

from __future__ import annotations

from rich.text import Text

from jarn.tui.widgets.diff import diff_from_edit_args, unified_diff_text

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _emphasis_substrings(text: Text) -> list[str]:
    """Return plain-text content of every span that carries bold+reverse emphasis."""
    result = []
    for span in text._spans:
        style = span.style
        if isinstance(style, str) and "bold" in style and "reverse" in style:
            result.append(text.plain[span.start:span.end])
    return result


# ---------------------------------------------------------------------------
# Named tests (brief)
# ---------------------------------------------------------------------------

def test_intraline_emphasis():
    """A one-word change renders emphasis only around the changed span.

    SequenceMatcher works at the character level, so the emphasis spans cover
    changed character runs inside "world"/"earth" rather than whole words.
    The key invariant: the unchanged prefix "hello " is never emphasised.
    """
    text = unified_diff_text("hello world", "hello earth")
    emphases = _emphasis_substrings(text)
    assert emphases, "expected emphasis spans for a one-word change"
    all_emph = "".join(emphases)
    # The unchanged prefix "hello " must NOT appear in any emphasis span
    assert "hello" not in all_emph, "unchanged prefix must not be emphasised"
    # At least some of the changed characters are present in the emphasis spans
    changed = set("world") | set("earth")
    assert any(c in all_emph for c in changed), "changed characters should be emphasised"


def test_unpaired_hunks_plain():
    """A hunk with 2 deletions and 3 additions (unequal counts) renders plain."""
    old = "line1\nline2"
    new = "newx\nnewy\nnewz"
    text = unified_diff_text(old, new)
    assert not _emphasis_substrings(text), (
        "unequal del/add counts must not produce any emphasis spans"
    )


def test_binary_unchanged():
    """diff_from_edit_args on a binary path returns the binary notice, no emphasis."""
    diff = diff_from_edit_args({"file_path": "image.png", "content": "<data>"})
    assert diff is not None
    assert "binary" in diff.plain
    assert not _emphasis_substrings(diff), "binary notice must carry no emphasis spans"


# ---------------------------------------------------------------------------
# Additional tests (brief addendum)
# ---------------------------------------------------------------------------

def test_long_line_skips_emphasis():
    """Lines longer than 200 characters fall back to plain line-level rendering."""
    long = "x" * 100 + "hello" + "x" * 100  # 205 chars
    text = unified_diff_text(long, long.replace("hello", "world"))
    assert not _emphasis_substrings(text), "lines >200 chars must not get emphasis"


def test_low_similarity_plain():
    """Line pairs with SequenceMatcher.ratio() < 0.3 render plain (no emphasis)."""
    # Zero characters in common → ratio = 0.0
    text = unified_diff_text("aaaaaaaaaa", "bbbbbbbbbb")
    assert not _emphasis_substrings(text), "low-similarity pairs must not get emphasis"


# ---------------------------------------------------------------------------
# Self-review: Rich markup escaping
# ---------------------------------------------------------------------------

def test_rich_markup_in_content_not_interpreted():
    """A diff line containing Rich-style markup tokens renders as literal text."""
    old = "x = [bold]value[/bold]"
    new = "x = [italic]value[/italic]"
    text = unified_diff_text(old, new)
    # Content must appear verbatim — brackets must not be consumed as markup
    assert "[bold]" in text.plain
    assert "[italic]" in text.plain


def test_colorize_unified_diff_reuses_line_colors():
    from jarn.tui.widgets.diff import colorize_unified_diff

    raw = "\n".join(
        [
            "diff --git a/x b/x",
            "--- a/x",
            "+++ b/x",
            "@@ -1 +1 @@",
            "-old",
            "+new",
        ]
    )
    text = colorize_unified_diff(raw)
    assert "old" in text.plain
    assert "new" in text.plain
    generated = unified_diff_text("old", "new", filename="x")
    colored = colorize_unified_diff(generated.plain)
    assert "old" in colored.plain and "new" in colored.plain


def _git_repo(root):
    import subprocess

    def g(*args):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)

    root.mkdir(parents=True, exist_ok=True)
    g("init", "-b", "main")
    g("config", "user.email", "test@jarn.test")
    g("config", "user.name", "Jarn Test")
    (root / "README.txt").write_text("init\n", encoding="utf-8")
    g("add", "README.txt")
    g("commit", "-m", "init")


def test_cmd_diff_staged_all_and_unknown(tmp_path, monkeypatch, base_config):
    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    _git_repo(root)
    (root / ".jarn").mkdir(parents=True)
    (root / "README.txt").write_text("init\nchanged\n", encoding="utf-8")
    ctrl = Controller(base_config, root)

    unknown = ctrl.handle_command("diff", "nope")
    assert "Usage:" in unknown.text

    working = ctrl.handle_command("diff", "all")
    assert "changed" in working.text or "+" in working.text

    import subprocess

    subprocess.run(["git", "add", "README.txt"], cwd=root, capture_output=True, check=True)
    staged = ctrl.handle_command("diff", "")
    assert "changed" in staged.text or "+" in staged.text
    explicit = ctrl.handle_command("diff", "staged")
    assert "changed" in explicit.text or "+" in explicit.text
    ctrl.close()


def test_cmd_diff_session_uses_recap_files(tmp_path, monkeypatch, base_config):
    import json

    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    _git_repo(root)
    (root / ".jarn").mkdir(parents=True)
    (root / "README.txt").write_text("init\nsession-edit\n", encoding="utf-8")
    ctrl = Controller(base_config, root)
    path = ctrl.sessions.transcript_path(ctrl.thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "tool",
                "name": "write_file",
                "args": {"file_path": "README.txt"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    text = ctrl.handle_command("diff", "session").text
    assert "session-edit" in text or "README.txt" in text
    ctrl.close()
