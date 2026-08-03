"""Regression tests for bounded, process-wide tokenizer loading."""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import jarn.memory.tokens as tokens


class _Encoder:
    def encode(self, text: str) -> list[str]:
        return text.split()


def _reset_loader(monkeypatch) -> None:
    monkeypatch.setattr(tokens, "_ENCODER", None)
    monkeypatch.setattr(tokens, "_ENCODER_FAILED", False)
    monkeypatch.setattr(tokens, "_ENCODER_WAITED", False)
    monkeypatch.setattr(tokens, "_ENCODER_WORKER", None)
    monkeypatch.setattr(tokens, "_ENCODER_RESULT", {})


def test_encoder_load_is_cached_after_failure(monkeypatch) -> None:
    _reset_loader(monkeypatch)
    calls = 0

    def _fail(_name: str) -> None:
        nonlocal calls
        calls += 1
        raise OSError("offline")

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=_fail))

    assert tokens.count_tokens("abcdefgh") == 2
    assert tokens.count_tokens("abcdefghijkl") == 3
    assert calls == 1


def test_encoder_load_timeout_is_bounded_and_not_repaid(monkeypatch) -> None:
    _reset_loader(monkeypatch)
    monkeypatch.setattr(tokens, "_ENCODER_LOAD_TIMEOUT", 0.02)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def _block(_name: str) -> _Encoder:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1.0)
        return _Encoder()

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=_block))

    before = time.monotonic()
    try:
        assert tokens.count_tokens("x" * 40) == 10
        elapsed = time.monotonic() - before
        assert started.is_set()
        assert elapsed < 0.2

        before = time.monotonic()
        assert tokens.count_tokens("x" * 20) == 5
        assert time.monotonic() - before < 0.02
        assert calls == 1
    finally:
        release.set()


def test_encoder_uses_persistent_jarn_cache_by_default(monkeypatch, tmp_path) -> None:
    _reset_loader(monkeypatch)
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "tiktoken",
        SimpleNamespace(get_encoding=lambda _name: _Encoder()),
    )

    assert tokens.count_tokens("hello world") == 2
    assert tokens.os.environ["TIKTOKEN_CACHE_DIR"] == str(tmp_path / "home" / "cache" / "tiktoken")


def test_encoder_preserves_explicit_cache_override(monkeypatch, tmp_path) -> None:
    _reset_loader(monkeypatch)
    explicit = tmp_path / "operator-cache"
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(explicit))
    monkeypatch.setitem(
        sys.modules,
        "tiktoken",
        SimpleNamespace(get_encoding=lambda _name: _Encoder()),
    )

    assert tokens.warm_tokenizer_cache(timeout=0.1)
    assert tokens.os.environ["TIKTOKEN_CACHE_DIR"] == str(explicit)


def _slow_encoder(release: threading.Event):
    def _get(_name: str) -> _Encoder:
        release.wait(3.0)
        return _Encoder()

    return _get


def test_slow_cold_load_is_picked_up_once_it_lands(monkeypatch) -> None:
    """A download that outlasts the budget must not pin the session to len // 4.

    A cold tiktoken fetch routinely takes longer than the 3 s wait, and treating
    that as a terminal verdict left every later count on the char heuristic —
    roughly 2x off on prose — for the life of the process.
    """
    _reset_loader(monkeypatch)
    monkeypatch.setattr(tokens, "_ENCODER_LOAD_TIMEOUT", 0.02)
    release = threading.Event()
    monkeypatch.setitem(
        sys.modules, "tiktoken", SimpleNamespace(get_encoding=_slow_encoder(release))
    )

    assert tokens.count_tokens("x" * 40) == 10  # gave up waiting -> fallback

    release.set()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and tokens._ENCODER is None:
        tokens.count_tokens("probe")
        time.sleep(0.01)

    assert tokens._ENCODER is not None, "a completed load must be adopted"
    assert tokens.count_tokens("a b c") == 3  # real encoder splits on whitespace


def test_warm_cache_survives_an_earlier_short_timeout(monkeypatch) -> None:
    """Setup's long warm must not inherit the verdict of a 3 s count."""
    _reset_loader(monkeypatch)
    monkeypatch.setattr(tokens, "_ENCODER_LOAD_TIMEOUT", 0.01)
    release = threading.Event()
    monkeypatch.setitem(
        sys.modules, "tiktoken", SimpleNamespace(get_encoding=_slow_encoder(release))
    )

    assert tokens.count_tokens("x" * 40) == 10  # timed out
    release.set()

    assert tokens.warm_tokenizer_cache(timeout=3.0) is True


def test_unwritable_cache_dir_is_left_to_tiktokens_default(monkeypatch, tmp_path) -> None:
    """Never claim a cache dir we cannot create.

    Setting TIKTOKEN_CACHE_DIR flips tiktoken's ``user_specified_cache``, which
    re-raises the cache-write errors its own tempdir default swallows — so
    claiming an unwritable directory converts a working fallback into no encoder.
    """
    _reset_loader(monkeypatch)
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.delenv("DATA_GYM_CACHE_DIR", raising=False)
    blocker = tmp_path / "home"
    blocker.write_text("JARN_HOME is a file, so mkdir under it fails", encoding="utf-8")
    monkeypatch.setenv("JARN_HOME", str(blocker))
    monkeypatch.setitem(
        sys.modules, "tiktoken", SimpleNamespace(get_encoding=lambda _n: _Encoder())
    )

    assert tokens.count_tokens("hello world") == 2
    assert "TIKTOKEN_CACHE_DIR" not in tokens.os.environ


def test_counting_survives_a_host_with_no_home(monkeypatch) -> None:
    """count_tokens is total — a missing home must not escape as RuntimeError."""
    _reset_loader(monkeypatch)
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.delenv("DATA_GYM_CACHE_DIR", raising=False)

    def _no_home():
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(tokens, "global_home", _no_home)
    monkeypatch.setitem(
        sys.modules, "tiktoken", SimpleNamespace(get_encoding=lambda _n: _Encoder())
    )

    assert tokens.count_tokens("hello world") == 2
    assert "TIKTOKEN_CACHE_DIR" not in tokens.os.environ


def test_pre_warmed_data_gym_cache_is_not_shadowed(monkeypatch, tmp_path) -> None:
    """tiktoken reads TIKTOKEN_CACHE_DIR first, so ours would hide the operator's."""
    _reset_loader(monkeypatch)
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.setenv("DATA_GYM_CACHE_DIR", str(tmp_path / "operator-cache"))
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setitem(
        sys.modules, "tiktoken", SimpleNamespace(get_encoding=lambda _n: _Encoder())
    )

    assert tokens.count_tokens("hello world") == 2
    assert "TIKTOKEN_CACHE_DIR" not in tokens.os.environ
