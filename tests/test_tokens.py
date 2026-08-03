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
    monkeypatch.setattr(tokens, "_ENCODER_LOAD_ATTEMPTED", False)


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


def test_encoder_load_timeout_is_bounded_and_negatively_cached(monkeypatch) -> None:
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
