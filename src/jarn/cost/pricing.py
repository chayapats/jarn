"""Model pricing & context-window resolution.

Both a model's USD price and its context-window size are resolved through the
same layered lookup, so the budget figure and the toolbar's ``ctx N%`` gauge
stay accurate across the whole model zoo — not just a handful of hardcoded names:

1. **User overrides** (offline, highest priority) — ``~/.jarn/pricing.yaml`` and
   ``~/.jarn/context_windows.yaml`` (substring-keyed, same shape as the built-in
   tables). Always win, so a user can correct anything.
2. **Built-in curated anchors** — the well-known headline models below. These are
   deterministic and offline, and keep the common case correct with no network.
3. **OpenRouter catalog** — the long tail. ``warm_catalog()`` fetches
   https://openrouter.ai/api/v1/models once (cached on disk under ``JARN_HOME``,
   24h TTL) so every model OpenRouter serves gets a real price + ``context_length``.
   The fetch runs in the background and fails *silently* — local / offline users
   are never blocked, and the hot path (called per token chunk) never touches the
   network: it only reads the in-memory / on-disk cache.

Unknown models cost 0 and are reported as "unpriced" (so the budget figure is
flagged as incomplete rather than silently wrong), and report a context window of
0 (so the toolbar hides ``ctx %`` rather than showing a guessed denominator).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from jarn.config import paths


@dataclass(frozen=True, slots=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float


# Curated anchors — kept deliberately small: the dedicated Anthropic API model
# ids (dash form, e.g. "claude-opus-4-8") and local models. Everything else is
# resolved from the OpenRouter catalog (the source of truth), so fictional /
# stale entries are intentionally NOT kept here. Keys match as substrings of the
# model id (longest wins), so "anthropic/claude-opus-4-8" matches "claude-opus-4-8".
_BUILTIN: dict[str, Price] = {
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-sonnet-4-5": Price(3.0, 15.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
    # Local models are free.
    "ollama": Price(0.0, 0.0),
    "lmstudio": Price(0.0, 0.0),
}

# Curated context windows (tokens), substring-keyed like the price table. Same
# scope: dedicated-Anthropic anchors + a sensible default for local Qwen-Coder.
CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "qwen3-coder": 256_000,
}


# -- substring tables (overrides + built-ins) -------------------------------


def _match_substr[T](table: dict[str, T], model_id: str) -> T | None:
    """Longest substring-key match in ``table`` for ``model_id`` (most specific)."""
    matches = [(k, v) for k, v in table.items() if k in model_id]
    if not matches:
        return None
    return max(matches, key=lambda kv: len(kv[0]))[1]


def _load_price_overrides() -> dict[str, Price]:
    path = paths.global_home() / "pricing.yaml"
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out: dict[str, Price] = {}
    for key, val in raw.items():
        if isinstance(val, dict) and "input" in val and "output" in val:
            out[key] = Price(float(val["input"]), float(val["output"]))
    return out


def _load_window_overrides() -> dict[str, int]:
    path = paths.global_home() / "context_windows.yaml"
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {k: int(v) for k, v in raw.items() if isinstance(v, (int, float))}


# -- OpenRouter catalog (the long tail) -------------------------------------

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CACHE_TTL_SECS = 24 * 3600
# In-memory memo for the parsed catalog (slug -> {input, output, context}).
# None = not yet loaded this process; we never memoize {} so a later warm can
# still populate it mid-session.
_MEM_CATALOG: dict[str, dict] | None = None


def _cache_file() -> Path:
    return paths.global_home() / "cache" / "openrouter_models.json"


def _read_disk_cache() -> dict | None:
    path = _cache_file()
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def _disk_cache_fresh() -> bool:
    path = _cache_file()
    try:
        return path.is_file() and (time.time() - path.stat().st_mtime) < _CACHE_TTL_SECS
    except OSError:
        return False


def _fetch_openrouter() -> dict[str, dict]:
    """Fetch + parse the OpenRouter model catalog. Returns {} on any failure."""
    try:
        import httpx

        resp = httpx.get(_OPENROUTER_MODELS_URL, timeout=10.0)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception:  # noqa: BLE001 - network/parse errors must never propagate
        return {}
    out: dict[str, dict] = {}
    for entry in data:
        mid = entry.get("id")
        if not mid:
            continue
        pr = entry.get("pricing") or {}
        try:  # OpenRouter quotes USD *per token* as strings, e.g. "0.000005".
            inp = float(pr.get("prompt", 0) or 0) * 1_000_000
            outp = float(pr.get("completion", 0) or 0) * 1_000_000
        except (TypeError, ValueError):
            inp = outp = 0.0
        ctx = entry.get("context_length") or 0
        out[mid] = {"input": inp, "output": outp, "context": int(ctx) if ctx else 0}
    return out


def _catalog() -> dict[str, dict]:
    """The cached catalog (in-memory, else on-disk). Never fetches — the hot
    path stays network-free; :func:`warm_catalog` refreshes it in the background."""
    global _MEM_CATALOG
    if _MEM_CATALOG is not None:
        return _MEM_CATALOG
    disk = _read_disk_cache()
    if disk is not None:
        _MEM_CATALOG = disk
        return disk
    return {}


def warm_catalog(force: bool = False) -> None:
    """Refresh the OpenRouter catalog cache (network). Safe to call in a daemon
    thread at startup; network/IO failures are swallowed so it never blocks."""
    global _MEM_CATALOG
    if not force and _disk_cache_fresh():
        if _MEM_CATALOG is None:
            _MEM_CATALOG = _read_disk_cache()
        return
    data = _fetch_openrouter()
    if not data:
        return
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    _MEM_CATALOG = data


# Dedicated-provider profiles whose name differs from the OpenRouter vendor
# prefix, so a ref like "mistral/mistral-large-2512" can still resolve against
# the catalog's "mistralai/mistral-large-2512".
_OR_VENDOR_ALIAS = {"mistral": "mistralai", "xai": "x-ai"}


def _catalog_entry(model_id: str) -> dict | None:
    """Exact catalog match for a model ref, trying the profile-stripped slug and a
    vendor-aliased slug too (``openrouter/anthropic/claude`` -> ``anthropic/claude``;
    ``mistral/mistral-large`` -> ``mistralai/mistral-large``)."""
    cat = _catalog()
    if not cat:
        return None
    if model_id in cat:
        return cat[model_id]
    if "/" in model_id:
        profile, rest = model_id.split("/", 1)
        if rest in cat:
            return cat[rest]
        alias = _OR_VENDOR_ALIAS.get(profile)
        if alias and f"{alias}/{rest}" in cat:
            return cat[f"{alias}/{rest}"]
    return None


# -- public resolution ------------------------------------------------------


def lookup(model_id: str) -> Price | None:
    """Resolve a model's price: user override -> curated -> OpenRouter catalog."""
    over = _match_substr(_load_price_overrides(), model_id)
    if over is not None:
        return over
    builtin = _match_substr(_BUILTIN, model_id)
    if builtin is not None:
        return builtin
    entry = _catalog_entry(model_id)
    if entry is not None:
        return Price(entry["input"], entry["output"])
    return None


def context_window(model_id: str) -> int:
    """Resolve a model's context window in tokens, or 0 when unknown.

    Order: user override -> curated -> OpenRouter catalog. 0 means "unknown" so
    callers can hide the gauge instead of dividing by a guessed denominator."""
    over = _match_substr(_load_window_overrides(), model_id)
    if over:
        return int(over)
    builtin = _match_substr(CONTEXT_WINDOWS, model_id)
    if builtin:
        return int(builtin)
    entry = _catalog_entry(model_id)
    if entry and entry.get("context"):
        return int(entry["context"])
    return 0


def cost_of(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    price = lookup(model_id)
    if price is None:
        return None
    return (
        input_tokens / 1_000_000 * price.input_per_mtok
        + output_tokens / 1_000_000 * price.output_per_mtok
    )
