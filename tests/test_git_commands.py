"""F5: /commit + /review diff gathering and seeded-prompt builders."""

from __future__ import annotations

import subprocess

import pytest

from jarn.agent.git_commands import (
    GitDiff,
    commit_prompt,
    gather_diff,
    review_prompt,
)


def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


# -- gather_diff -------------------------------------------------------------

def test_gather_non_git(tmp_path):
    d = gather_diff(tmp_path)
    assert d.is_repo is False
    assert not d.has_changes


def test_gather_clean_repo(repo):
    d = gather_diff(repo)
    assert d.is_repo is True
    assert not d.has_changes


def test_gather_staged(repo):
    (repo / "a.txt").write_text("one\ntwo\n")
    _git(repo, "add", "a.txt")
    d = gather_diff(repo)
    assert d.is_repo and d.has_staged and d.has_changes
    assert "two" in d.staged


def test_gather_unstaged_only(repo):
    (repo / "a.txt").write_text("one\nthree\n")
    d = gather_diff(repo)
    assert d.is_repo and not d.has_staged and d.has_changes
    assert "three" in d.unstaged


# -- commit_prompt -----------------------------------------------------------

def test_commit_prompt_non_repo():
    assert commit_prompt(GitDiff(is_repo=False, staged="", unstaged="", status="")) is None


def test_commit_prompt_clean():
    assert commit_prompt(GitDiff(is_repo=True, staged="", unstaged="", status="")) is None


def test_commit_prompt_staged():
    d = GitDiff(is_repo=True, staged="diff --git a/a.txt ...\n+two\n", unstaged="", status="M a.txt")
    p = commit_prompt(d)
    assert p is not None
    assert "git commit" in p
    assert "+two" in p
    assert "staged" in p.lower()


def test_commit_prompt_unstaged_instructs_staging():
    d = GitDiff(is_repo=True, staged="", unstaged="+three\n", status=" M a.txt")
    p = commit_prompt(d)
    assert p is not None
    assert "git add" in p
    assert "+three" in p


# -- review_prompt -----------------------------------------------------------

def test_review_prompt_changes():
    d = GitDiff(is_repo=True, staged="+two\n", unstaged="+three\n", status="")
    p = review_prompt(d)
    assert p is not None
    assert "read-only" in p.lower()
    assert "+two" in p and "+three" in p


def test_review_prompt_clean_is_none():
    assert review_prompt(GitDiff(is_repo=True, staged="", unstaged="", status="")) is None


def test_review_prompt_non_repo_is_none():
    assert review_prompt(GitDiff(is_repo=False, staged="", unstaged="", status="")) is None


# -- truncation --------------------------------------------------------------

def test_large_diff_truncated():
    big = "+" + ("x" * 50000) + "\n"
    p = commit_prompt(GitDiff(is_repo=True, staged=big, unstaged="", status="M big"))
    assert p is not None
    assert "diff truncated" in p
    assert len(p) < 40000
