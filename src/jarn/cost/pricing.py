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
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from jarn.config import paths

logger = logging.getLogger("jarn.cost")

# Set of model ids for which the unpriced notice has already been emitted this
# process lifetime.  Kept at module level so repeated calls never re-warn.
_WARNED_UNPRICED: set[str] = set()


def warn_unpriced(model_id: str) -> None:
    """Record (once per model id) that no price was found for *model_id*.

    Routed to the ``jarn`` file logger, NOT ``warnings.warn``: in the TUI a raw
    Python warning leaks to stderr and corrupts the display mid-session (it reads
    like an error). The unpriced state is still surfaced cleanly to the user via
    ``/cost`` (the ``unpriced`` call count). Deduped per model id so the
    high-frequency cost path never repeats it.
    """
    if model_id in _WARNED_UNPRICED:
        return
    _WARNED_UNPRICED.add(model_id)
    logger.warning("No price for %s — cost will be counted as $0", model_id)


@dataclass(frozen=True, slots=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    # Optional prompt-cache rates ($/Mtok). ``None`` (the default) means "price
    # like uncached input" — so adding cache accounting never changes the total
    # for a price table that doesn't declare cache rates. Cache reads are usually
    # ~0.1x input and cache writes ~1.25x input, but those are provider-specific,
    # so we don't guess: a price source must opt in to override the fallback.
    cache_read_rate: float | None = None
    cache_write_rate: float | None = None


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


def cost_of(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float | None:
    """USD cost of one call, or ``None`` if the model is unpriced.

    ``input_tokens`` is the provider's *full* reported input — LangChain's
    ``usage_metadata['input_tokens']`` already folds the prompt-cache counts back
    into the total (Anthropic's raw API excludes them; LangChain adds them back).
    ``cache_read_tokens`` / ``cache_creation_tokens`` are therefore the cached
    *subset* of that input: they are subtracted from the plain-input charge and
    repriced at the model's explicit cache rate when set, else at the plain input
    rate — so a cached token is counted once, never billed at the input rate AND
    as a cache line. With both at 0 (no cache usage), the result is exactly the
    original ``input + output`` figure — totals never drift.
    """
    price = lookup(model_id)
    if price is None:
        warn_unpriced(model_id)
        return None
    cache_read_rate = (
        price.cache_read_rate if price.cache_read_rate is not None else price.input_per_mtok
    )
    cache_write_rate = (
        price.cache_write_rate if price.cache_write_rate is not None else price.input_per_mtok
    )
    # Cache tokens are a subset of input_tokens — charge the uncached remainder at
    # the input rate and the cached portions at their own rates (no double-count).
    plain_input = max(0, input_tokens - cache_read_tokens - cache_creation_tokens)
    return (
        plain_input / 1_000_000 * price.input_per_mtok
        + output_tokens / 1_000_000 * price.output_per_mtok
        + cache_read_tokens / 1_000_000 * cache_read_rate
        + cache_creation_tokens / 1_000_000 * cache_write_rate
    )
