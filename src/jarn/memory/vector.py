"""Semantic recall over markdown memories.

Layered *on top of* the file-based store (which remains the source of truth). The
default embedder is fully local and deterministic — a hashed bag-of-words vector
— so recall works offline and in tests with no API calls. A provider embedder
(any LangChain ``Embeddings``) can be plugged in via config for higher quality.

Embeddings are cached to ``<memory-dir>/.vectors.json`` keyed by a content hash,
so re-embedding only happens when a memory's text changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jarn.memory.store import Memory, MemoryStore, slugify

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DIM = 256


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...
    @property
    def name(self) -> str: ...


class LocalEmbedder:
    """Deterministic, dependency-free hashed bag-of-words embedding.

    Tokens are hashed into a fixed-dim vector with sublinear term weighting and
    L2 normalization. Not as good as a neural embedder, but stable, instant, and
    offline — a sensible default for lexical-semantic recall.
    """

    name = "local-hash-v1"

    def __init__(self, dim: int = _DIM) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        # Hash both whole words and character 3-grams so morphological variants
        # ("test"/"tests"/"testing") share subword features — much better recall
        # than whole-word hashing alone, while staying dependency-free.
        counts: dict[int, float] = {}
        for tok in _TOKEN_RE.findall(text.lower()):
            self._add(counts, tok, weight=1.0)
            padded = f"#{tok}#"
            for i in range(len(padded) - 2):
                self._add(counts, padded[i : i + 3], weight=0.5)
        vec = [0.0] * self.dim
        for idx, c in counts.items():
            sign = 1.0 if (idx >> 1) & 1 else -1.0
            vec[idx] = sign * (1.0 + math.log(c))
        return _normalize(vec)

    def _add(self, counts: dict[int, float], token: str, *, weight: float) -> None:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        idx = h % self.dim
        counts[idx] = counts.get(idx, 0.0) + weight


@dataclass(slots=True)
class ProviderEmbedder:
    """Wraps a LangChain ``Embeddings`` instance."""

    embeddings: object
    name: str = "provider"

    def embed(self, text: str) -> list[float]:
        vec = self.embeddings.embed_query(text)  # type: ignore[attr-defined]
        return _normalize(list(vec))


@dataclass(slots=True, frozen=True)
class RecallHit:
    memory: Memory
    score: float


class VectorIndex:
    """In-memory cosine index over a :class:`MemoryStore`."""

    def __init__(self, store: MemoryStore, embedder: Embedder | None = None) -> None:
        self.store = store
        self.embedder = embedder or LocalEmbedder()
        self._vectors: dict[str, list[float]] = {}
        self._memories: dict[str, Memory] = {}

    @property
    def _cache_path(self) -> Path:
        return self.store.root / ".vectors.json"

    def build(self) -> int:
        """(Re)build the index, reusing cached vectors when text is unchanged."""
        cache = self._load_cache()
        new_cache: dict[str, dict] = {}
        self._vectors.clear()
        self._memories.clear()
        for mem in self.store.load_all():
            text = f"{mem.name}\n{mem.description}\n{mem.body}"
            key = mem.name
            digest = hashlib.md5(text.encode()).hexdigest()
            entry = cache.get(key)
            if entry and entry.get("digest") == digest and entry.get("embedder") == self.embedder.name:
                vec = entry["vector"]
            else:
                vec = self.embedder.embed(text)
            self._vectors[key] = vec
            self._memories[key] = mem
            new_cache[key] = {"digest": digest, "embedder": self.embedder.name, "vector": vec}
        self._save_cache(new_cache)
        return len(self._vectors)

    def search(self, query: str, k: int = 5) -> list[RecallHit]:
        if not self._vectors:
            self.build()
        if not self._vectors:
            return []
        qv = self.embedder.embed(query)
        scored = [
            RecallHit(self._memories[key], _cosine(qv, vec))
            for key, vec in self._vectors.items()
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return [h for h in scored[:k] if h.score > 0.0]

    def _load_cache(self) -> dict:
        if self._cache_path.is_file():
            try:
                return json.loads(self._cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_cache(self, cache: dict) -> None:
        if not self.store.root.is_dir():
            return
        import contextlib

        with contextlib.suppress(OSError):
            self._cache_path.write_text(json.dumps(cache), encoding="utf-8")


def recall_block(
    query: str,
    k: int = 3,
    *,
    project_root: Path | None = None,
    include_project: bool = True,
) -> str:
    """Return a prompt block of the most relevant memories for ``query``.

    Searches global memory and, when trusted, project memory. Empty string if
    nothing relevant.
    """
    hits: list[RecallHit] = []
    stores = [MemoryStore.global_store()]
    project = MemoryStore.project_store(project_root) if include_project else None
    if project:
        stores.append(project)
    for store in stores:
        if store.root.is_dir():
            hits.extend(VectorIndex(store).search(query, k))
    deduped: dict[str, RecallHit] = {}
    for hit in hits:
        key = slugify(hit.memory.name)
        existing = deduped.get(key)
        if existing is None or hit.score > existing.score:
            deduped[key] = hit
    top = sorted(deduped.values(), key=lambda h: h.score, reverse=True)[:k]
    if not top:
        return ""
    lines = ["# Relevant memories"]
    for hit in top:
        lines.append(f"- **{hit.memory.name}** — {hit.memory.description}")
    return "\n".join(lines)


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))
