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
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    # Anthropic prompt-cache multipliers over input: read = 0.1x, write (5m) = 1.25x.
    "claude-opus-4-8": Price(5.0, 25.0, cache_read_rate=0.5, cache_write_rate=6.25),
    "claude-sonnet-4-5": Price(3.0, 15.0, cache_read_rate=0.3, cache_write_rate=3.75),
    "claude-haiku-4-5": Price(1.0, 5.0, cache_read_rate=0.1, cache_write_rate=1.25),
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


def _norm_slug(s: str) -> str:
    """Fold a version separator so dot- and dash-form slugs match interchangeably.

    OpenRouter quotes model versions with a DOT (``claude-opus-4.8``) while the
    dedicated Anthropic API — and therefore our curated anchors — use a DASH
    (``claude-opus-4-8``). They are different namespaces for the same model, so a
    dot-form ref (the shipped OpenRouter default) would otherwise miss every
    dash-keyed anchor and price at $0 offline. Normalizing dots to dashes bridges
    the two without duplicating every anchor. Only exact catalog lookups
    (``_catalog_entry``) stay separator-sensitive; those key off OpenRouter's own
    dot-form ids so no bridging is needed there."""
    return s.replace(".", "-")


def _match_substr[T](table: dict[str, T], model_id: str) -> T | None:
    """Longest substring-key match in ``table`` for ``model_id`` (most specific).

    Matching is dot/dash-insensitive on the version separator (see
    :func:`_norm_slug`) so a dash-keyed curated anchor still matches a dot-form
    ref and vice-versa; the longest-match tiebreak uses the original key length."""
    norm_id = _norm_slug(model_id)
    matches = [(k, v) for k, v in table.items() if _norm_slug(k) in norm_id]
    if not matches:
        return None
    return max(matches, key=lambda kv: len(kv[0]))[1]


# mtime-memoized override parses, keyed by absolute path string. The override
# loaders run on the per-token cost hot path (twice per ``tracker.record`` under
# the tracker lock), so re-reading + re-parsing the YAML on every lookup is a
# needless syscall+parse storm; this caches the parsed result and re-parses only
# when the file changes.
_YAML_CACHE: dict[str, tuple[int, Any]] = {}


def _cached_load(path: Path, loader: Callable[[Path], Any]) -> Any:
    """Return ``loader(path)`` memoized by ``path``'s ``st_mtime_ns``.

    Invariant: the parse is recomputed only when the file's mtime changes.
    ``st_mtime_ns`` (not ``st_mtime``) is required so a rewrite within the same
    wall-clock second — as in the tests — still invalidates the cache. A missing
    file (``stat`` raises ``OSError``) yields ``{}`` and is never cached."""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    hit = _YAML_CACHE.get(str(path))
    if hit is not None and hit[0] == mtime_ns:
        return hit[1]
    data = loader(path)
    _YAML_CACHE[str(path)] = (mtime_ns, data)
    return data


def _valid_rate(x: Any) -> float:
    """Parse a USD rate, rejecting bool / NaN / inf / negative values.

    ``float()`` alone happily accepts ``.nan``, ``.inf`` and negatives — but a NaN
    rate poisons the whole cost to NaN, and since *every* comparison against NaN is
    False, a configured hard-stop budget would then NEVER fire. Require a finite,
    non-negative value so an invalid rate raises ``ValueError`` (and is skipped)
    rather than silently disabling the budget guard.

    ``bool`` is rejected explicitly BEFORE ``float()``: ``float(False) == 0.0`` and
    ``float(True) == 1.0`` would otherwise admit a boolean as a valid rate — a bool
    in a required-rate field means the value is unknown, not a legitimate $0."""
    if isinstance(x, bool):
        raise ValueError(f"rate must be a number, not a bool, got {x!r}")
    v = float(x)
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"rate must be finite and non-negative, got {x!r}")
    return v


def _safe_cache_rate(raw: Any, slug: str) -> float | None:
    """Validate an OPTIONAL cache rate ($/Mtok) already in processed form.

    An absent rate is ``None`` (fall back to input). An invalid one (NaN / inf /
    negative / unparseable) is warned about and treated as absent rather than
    propagated into cost — never a hard error, since the rate is optional."""
    if raw is None:
        return None
    try:
        return _valid_rate(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid cache rate for %r; treating as absent", slug)
        return None


def _validate_catalog(raw: Any) -> dict[str, dict]:
    """Validate a *processed* catalog mapping (slug -> {input, output, context,
    cache_read?, cache_write?}), dropping any invalid entry with a warning.

    This is the SINGLE per-entry boundary shared by both catalog sources — the
    network fetch (post-conversion) and the on-disk cache — so a poisoned value
    (e.g. a legacy JSON ``NaN`` rate, which every comparison treats as False and
    would silently disable a hard-stop budget) can never reach ``cost_of`` from
    either path. A non-mapping root yields ``{}``."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for slug, entry in raw.items():
        # A non-string / empty id would break substring lookups (``k in model_id``).
        if not isinstance(slug, str) or not slug:
            logger.warning("Ignoring catalog entry with non-string id %r", slug)
            continue
        if not isinstance(entry, dict):
            logger.warning("Ignoring non-mapping catalog entry for %r", slug)
            continue
        # Required rates: invalid (missing / NaN / inf / negative) -> SKIP the whole
        # entry so the model stays UNPRICED (counted by the tracker), never admitted
        # at $0 which would leave a hard-stop budget OK under unlimited real usage.
        try:
            inp = _valid_rate(entry.get("input"))
            outp = _valid_rate(entry.get("output"))
        except (TypeError, ValueError):
            logger.warning("Ignoring catalog entry %r with invalid required rate", slug)
            continue
        ctx_raw = entry.get("context") or 0
        try:
            ctx = int(ctx_raw) if ctx_raw else 0
        except (TypeError, ValueError):
            ctx = 0
        out[slug] = {
            "input": inp,
            "output": outp,
            "context": ctx,
            "cache_read": _safe_cache_rate(entry.get("cache_read"), slug),
            "cache_write": _safe_cache_rate(entry.get("cache_write"), slug),
        }
    return out


def _parse_price_overrides(path: Path) -> dict[str, Price]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        # Swallow but name the file: a typo in pricing.yaml would otherwise
        # discard every override silently, leaving the user's prices unapplied.
        logger.warning("Ignoring unparseable price overrides at %s", path)
        return {}
    # Only an EMPTY document (None) maps silently to {}. A valid-YAML but
    # non-mapping root (a top-level list, or a FALSY scalar like [], 0, false)
    # has no .items(): ``or {}`` would coerce the falsy ones to {} without a
    # warning — name the file and bail so the user sees why nothing applied.
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring non-mapping price overrides at %s", path)
        return {}
    out: dict[str, Price] = {}
    for key, val in raw.items():
        # A non-string key (e.g. a numeric YAML key ``1:``) would be admitted here
        # and later crash every _match_substr lookup (``k in model_id`` needs a str
        # left operand). Reject it inside the per-entry boundary — skip only it.
        if not isinstance(key, str):
            logger.warning(
                "Ignoring non-string price override key %r in pricing.yaml", key
            )
            continue
        # A dict entry missing (or misspelling) the required input/output keys —
        # or a non-mapping value entirely — must warn + skip, never silently drop
        # so a typo leaves the user wondering why their price never applied.
        if not (isinstance(val, dict) and "input" in val and "output" in val):
            logger.warning(
                "Ignoring price override for %r in pricing.yaml (needs input+output)", key
            )
            continue
        # Per-entry boundary: a bad number in one entry (e.g. cache_read: nope, or a
        # non-finite .nan/.inf/negative rate) must skip only that entry, never
        # discard every other override — and the rate conversions (via _valid_rate,
        # which also rejects NaN/inf/negative) must live INSIDE the boundary.
        try:
            cr = val.get("cache_read")
            cw = val.get("cache_write")
            out[key] = Price(
                _valid_rate(val["input"]),
                _valid_rate(val["output"]),
                cache_read_rate=_valid_rate(cr) if cr is not None else None,
                cache_write_rate=_valid_rate(cw) if cw is not None else None,
            )
        except (TypeError, ValueError):
            logger.warning("Ignoring bad price override for %r in pricing.yaml", key)
    return out


def _load_price_overrides() -> dict[str, Price]:
    return _cached_load(paths.global_home() / "pricing.yaml", _parse_price_overrides)


def _parse_window_overrides(path: Path) -> dict[str, int]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        logger.warning("Ignoring unparseable context-window overrides at %s", path)
        return {}
    # Only an EMPTY document (None) maps silently to {}. A non-mapping root (a
    # top-level list, or a falsy scalar like [], 0, false) has no .items():
    # ``or {}`` would hide the falsy ones — warn and bail instead.
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring non-mapping context-window overrides at %s", path)
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        # A non-string key would later crash _match_substr (``k in model_id``).
        if not isinstance(k, str):
            logger.warning("Ignoring non-string context-window override key %r", k)
            continue
        # Per-entry boundary: one unparseable window must not discard the rest.
        # A window must be a POSITIVE token count — 0 / negative is invalid (a
        # 1-token or 0 window elsewhere collapses the compact budget to 0), so
        # ``int(v) > 0`` is required; anything else warns + skips only that entry.
        try:
            iv = int(v)
            if iv <= 0:
                raise ValueError(f"context window must be positive, got {iv}")
            out[k] = iv
        except (TypeError, ValueError):
            logger.warning("Ignoring bad context-window override for %r", k)
    return out


def _load_window_overrides() -> dict[str, int]:
    return _cached_load(
        paths.global_home() / "context_windows.yaml", _parse_window_overrides
    )


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
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    # A disk cache is untrusted (a legacy or hand-edited file may carry JSON NaN):
    # route it through the SAME per-entry validator as the network fetch so a
    # poisoned entry is dropped with a warning before it can memoize into cost.
    return _validate_catalog(raw)


def _disk_cache_fresh() -> bool:
    path = _cache_file()
    try:
        return path.is_file() and (time.time() - path.stat().st_mtime) < _CACHE_TTL_SECS
    except OSError:
        return False


def _per_mtok(raw: Any) -> float | None:
    """OpenRouter per-token USD (string) -> $/Mtok, or ``None`` when absent.

    Same unit convention as ``prompt``/``completion``; an absent or unparseable
    field stays ``None`` so cost_of falls back to the plain input rate."""
    if raw is None:
        return None
    try:
        # _valid_rate rejects NaN/inf/negative too, so a poisoned catalog rate is
        # treated as absent (None) rather than propagated into cost as NaN.
        return _valid_rate(raw) * 1_000_000
    except (TypeError, ValueError):
        return None


def _fetch_openrouter() -> dict[str, dict]:
    """Fetch + parse the OpenRouter model catalog. Returns {} on any failure."""
    try:
        import httpx

        resp = httpx.get(_OPENROUTER_MODELS_URL, timeout=10.0)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:  # noqa: BLE001 - network/parse errors must never propagate
        # Never propagate, but do not swallow silently: a failed fetch leaves the
        # long tail unpriced, and the user deserves a breadcrumb for why.
        logger.warning(
            "OpenRouter catalog fetch failed (%s); long-tail models stay unpriced", exc
        )
        return {}
    out: dict[str, dict] = {}
    # A non-list ``data`` (malformed response) has no per-entry iteration to do:
    # bail rather than blow up in the loop.
    if not isinstance(data, list):
        logger.warning("OpenRouter catalog 'data' is not a list; long-tail stays unpriced")
        return {}
    for entry in data:
        # Per-entry boundary: one malformed record — a non-mapping element, or
        # context_length: "unknown" — must skip only that model, never abort the
        # whole healthy catalog and kill the background pricing thread. EVERYTHING
        # per-entry (mapping check, id extraction, rate/context parsing) lives
        # INSIDE the try so a bad element can't crash the loop before the boundary.
        mid: Any = None
        try:
            if not isinstance(entry, dict):
                raise TypeError("catalog entry is not a mapping")
            mid = entry.get("id")
            # Require a non-empty STRING id: a non-string id would later crash every
            # substring lookup (``k in model_id``); an empty one is unusable.
            if not isinstance(mid, str) or not mid:
                raise ValueError("catalog entry id is missing or non-string")
            pr = entry.get("pricing")
            if not isinstance(pr, dict):
                raise TypeError("catalog entry pricing is not a mapping")
            # OpenRouter quotes USD *per token* as strings, e.g. "0.000005". The
            # REQUIRED prompt/completion rates are checked by KEY PRESENCE and passed
            # RAW (no default, no ``or 0``): a `.get(..., 0) or 0` would coerce a
            # missing / empty / bool rate into a valid $0, silently pricing an unknown
            # model — which leaves a hard-stop budget OK under unlimited real usage.
            # CRITICAL DISTINCTION: a PRESENT rate of "0"/0 is a legitimate free-model
            # price (OpenRouter ``:free`` variants) and MUST stay priced at $0; a
            # MISSING key means unknown, so raise -> the whole entry is SKIPPED and the
            # model stays UNPRICED (counted by the tracker). An invalid present value
            # (empty / NaN / inf / negative / bool / unparseable) likewise raises here.
            if "prompt" not in pr or "completion" not in pr:
                raise ValueError("catalog entry pricing missing required prompt/completion")
            inp = _valid_rate(pr.get("prompt")) * 1_000_000
            outp = _valid_rate(pr.get("completion")) * 1_000_000
            ctx = entry.get("context_length") or 0
            out[mid] = {
                "input": inp,
                "output": outp,
                "context": int(ctx) if ctx else 0,
                # Optional cache rates: invalid -> absent (None) via _per_mtok.
                "cache_read": _per_mtok(pr.get("input_cache_read")),
                "cache_write": _per_mtok(pr.get("input_cache_write")),
            }
        except (TypeError, ValueError, AttributeError):
            # Name the id if we got a usable one, else a truncated repr of the raw element.
            label = mid if isinstance(mid, str) and mid else repr(entry)[:80]
            logger.warning("Ignoring malformed OpenRouter catalog entry %r", label)
    # Final guard: route the converted catalog through the SAME per-entry validator
    # the disk cache uses, so both paths share one finiteness/shape boundary.
    return _validate_catalog(out)


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


def warm_catalog(force: bool = False, *, network: bool = True) -> None:
    """Refresh the OpenRouter catalog cache (network). Safe to call in a daemon
    thread at startup; network/IO failures are swallowed so it never blocks.

    Skips the fetch when ``network`` is ``False`` or env
    ``JARN_NO_NETWORK_PRICING=1`` is set.
    """
    if not network_fetch_enabled(config_network=network):
        return
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


def network_fetch_enabled(*, config_network: bool = True) -> bool:
    """Whether OpenRouter catalog network fetch is allowed."""
    import os

    token = os.environ.get("JARN_NO_NETWORK_PRICING", "").strip().lower()
    if token in ("1", "true", "yes", "on"):
        return False
    return config_network


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
        # ``.get`` so a catalog cached before cache rates were parsed still resolves.
        return Price(
            entry["input"],
            entry["output"],
            cache_read_rate=entry.get("cache_read"),
            cache_write_rate=entry.get("cache_write"),
        )
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
