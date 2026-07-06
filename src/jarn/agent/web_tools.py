"""Built-in web tools: ``web_search`` and ``web_fetch``.

These give the agent real read-only internet access (the "Web search + fetch"
v1 capability). They are dependency-light (httpx + stdlib regex; no bs4) and
fail gracefully with a readable message rather than raising, so a flaky network
never crashes a turn.

``web_search`` is pluggable: set ``search.provider`` in config to ``tavily``,
``brave``, or ``exa`` for higher-quality results, or leave it as ``auto``
(default) to auto-discover a provider from environment variables / keychain
(falling back to the keyless DuckDuckGo scraper when none are set).

Provider API hosts contacted (all HTTPS, no SSRF guard — fixed trusted
endpoints, not user-supplied URLs):
  * api.tavily.com          — Tavily Search API
  * api.search.brave.com   — Brave Search API
  * api.exa.ai             — Exa Search API
  * html.duckduckgo.com    — DuckDuckGo HTML scraper (keyless fallback, SSRF-guarded)
"""

from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx
from langchain_core.tools import tool

if TYPE_CHECKING:
    from jarn.config.schema import Config

_UA = "Mozilla/5.0 (compatible; JARN/0.1; +https://github.com/chayapats/jarn)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")

# ---------------------------------------------------------------------------
# Pluggable provider configuration
# ---------------------------------------------------------------------------

#: Conventional per-provider environment variable names.
PROVIDER_ENV: dict[str, str] = {
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_API_KEY",
    "exa": "EXA_API_KEY",
}

#: Provider API endpoints (fixed trusted hosts — NOT user-supplied, not SSRF-guarded).
_TAVILY_URL = "https://api.tavily.com/search"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_EXA_URL = "https://api.exa.ai/search"

#: Active config — set by :func:`build_web_tools` at session start.
_active_config: Config | None = None

#: Hard cap on bytes downloaded by web_fetch (DoS / memory guard).
_MAX_FETCH_BYTES = 2_000_000
#: Maximum redirect hops to follow (each re-validated for SSRF).
_MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}


# -- SSRF guard -------------------------------------------------------------

def _allowlisted_hosts() -> set[str]:
    """Hosts the user has explicitly opted to allow (comma-separated env var)."""
    raw = os.environ.get("JARN_WEB_FETCH_ALLOW_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _resolve_ips(host: str) -> list[str]:
    """Resolve a hostname to its IP strings. Separated so tests can stub DNS."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


#: Carrier-grade NAT (RFC 6598) — not flagged private by ``ipaddress`` but
#: routable only inside provider/k8s networks, so block it too.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → refuse
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NET:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local      # covers 169.254.0.0/16 cloud metadata
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _check_host(host: str) -> tuple[list[str], str | None]:
    """Return ``(resolved_ips, block_reason)`` for *host*.

    ``block_reason`` is ``None`` when the host is safe to fetch; otherwise it
    explains why it was refused (and ``resolved_ips`` may be empty). Blocks
    loopback / private / link-local / reserved targets (so the agent can't reach
    localhost, the LAN, or the cloud-metadata endpoint), unless the host is on the
    explicit allowlist. Both literal IPs and DNS names (every resolved address)
    are checked.

    The resolved IPs are returned so the caller can **pin the very same address**
    it validated at connect-time — DNS is resolved exactly once, eliminating the
    rebinding window where a second lookup could return a different (private) IP.
    For an allowlisted host the IPs are still resolved and returned so pinning
    works, but a block is never raised.
    """
    if not host:
        return [], "missing host"
    allowlisted = host.lower() in _allowlisted_hosts()
    try:
        ipaddress.ip_address(host)
        ips = [host]                       # literal IP — no DNS needed
    except ValueError:
        try:
            ips = _resolve_ips(host)
        except OSError:
            return [], f"could not resolve host {host!r}"
    if allowlisted:
        return ips, None
    blocked = [ip for ip in ips if _ip_is_blocked(ip)]
    if blocked:
        return ips, f"refusing to reach a private/loopback address ({blocked[0]})"
    return ips, None


@dataclass(slots=True)
class _RawResponse:
    status_code: int
    headers: dict[str, str]
    text: str


def _format_pinned_netloc(ip: str, port: int, default_port: int) -> str:
    """Format a pinned IP for URL netloc (IPv6 must be bracketed)."""
    addr = ipaddress.ip_address(ip.strip("[]"))
    host = f"[{addr}]" if isinstance(addr, ipaddress.IPv6Address) else str(addr)
    if port == default_port:
        return host
    return f"{host}:{port}"


def _pinned_request(url: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return (connect_url, headers, extensions) with the resolved IP pinned."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    # Resolve + validate in ONE call and pin the exact IP we just checked. No
    # second DNS lookup happens, so there is no rebinding window between the
    # safety check and the connect.
    ips, reason = _check_host(hostname)
    if reason:
        raise ValueError(reason)
    if not ips:
        raise ValueError(f"could not resolve host {hostname!r}")
    pinned_ip = ips[0]

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = _format_pinned_netloc(pinned_ip, port, default_port)
    connect_url = urlunparse(parsed._replace(netloc=netloc))
    headers = {"User-Agent": _UA, "Host": hostname}
    extensions: dict[str, str] = {}
    if parsed.scheme == "https":
        extensions["sni_hostname"] = hostname
    return connect_url, headers, extensions


def _fetch_raw(url: str, *, max_bytes: int = _MAX_FETCH_BYTES) -> _RawResponse:
    """One HTTP GET with a hard byte cap, no auto-redirects. Network seam that
    tests stub out so the SSRF/redirect logic can be exercised offline.

    Connects to the IP validated by :func:`_check_host` (with the original
    ``Host`` header and TLS SNI) so DNS cannot rebind between check and fetch.
    """
    connect_url, headers, extensions = _pinned_request(url)
    # Module-level httpx.stream() omits ``extensions``; Client.stream passes
    # sni_hostname through to httpcore for TLS when connecting to a pinned IP.
    with httpx.Client() as client, client.stream(
        "GET",
        connect_url,
        headers=headers,
        extensions=extensions,
        timeout=20,
        follow_redirects=False,
    ) as resp:
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        raw = b"".join(chunks)
        encoding = resp.encoding or "utf-8"
        return _RawResponse(
            resp.status_code, dict(resp.headers), raw.decode(encoding, errors="replace")
        )
# ---------------------------------------------------------------------------
# Shared result formatter
# ---------------------------------------------------------------------------

def _format_results(query: str, items: list[tuple[str, str, str]]) -> str:
    """Format (title, url, snippet) tuples into the standard web_search output.

    This is the single source of truth for the output format — every provider
    routes through here so prompts don't change when the backend changes.
    """
    results: list[str] = []
    for title, url, snippet in items:
        line = f"- {title}\n  {url}"
        if snippet:
            line += f"\n  {snippet}"
        results.append(line)
    if not results:
        return f"No results for {query!r} (or the search page format changed)."
    return f"Top results for {query!r}:\n\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------

def _resolve_provider_key(provider: str, cfg: Config | None) -> str | None:
    """Resolve the API key for *provider* following the three-step priority order.

    1. ``cfg.search.api_key`` — only when non-empty AND the config explicitly
       names this provider (i.e. ``cfg.search.provider == provider``).
    2. Conventional env-var reference ``${PROVIDER_API_KEY}``.
    3. Keychain entry ``keychain:jarn/<provider>``.

    Returns ``None`` if no key resolves.  ``SecretResolutionError`` is swallowed
    and treated as "unresolved" so callers don't need to handle it.
    """
    from jarn.config.secrets import SecretResolutionError, resolve

    # Step 1: explicit search.api_key for the named provider
    if cfg is not None:
        api_key = cfg.search.api_key
        if api_key and cfg.search.provider.value == provider:
            try:
                resolved = resolve(api_key)
                if resolved:
                    return resolved
            except SecretResolutionError:
                pass

    # Step 2: conventional per-provider env var
    env_ref = f"${{{PROVIDER_ENV[provider]}}}"
    try:
        resolved = resolve(env_ref)
        if resolved:
            return resolved
    except SecretResolutionError:
        pass

    # Step 3: keychain entry
    keychain_ref = f"keychain:jarn/{provider}"
    try:
        resolved = resolve(keychain_ref)
        if resolved:
            return resolved
    except SecretResolutionError:
        pass

    return None


# ---------------------------------------------------------------------------
# HTTP client factory (interceptable in tests via monkeypatch)
# ---------------------------------------------------------------------------

def _api_client() -> httpx.Client:
    """Return a new httpx Client for provider API calls (10 s timeout).

    This is a standalone function so tests can monkeypatch it to inject a
    MockTransport without touching production code paths.
    """
    return httpx.Client(timeout=10)


# ---------------------------------------------------------------------------
# Provider-specific search clients
# ---------------------------------------------------------------------------

def _tavily_search(query: str, max_results: int, key: str) -> list[tuple[str, str, str]]:
    """Call the Tavily Search API. Raises on HTTP error."""
    with _api_client() as client:
        resp = client.post(
            _TAVILY_URL,
            json={"api_key": key, "query": query, "max_results": max_results},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    items: list[tuple[str, str, str]] = []
    for r in data.get("results", [])[:max_results]:
        title = r.get("title") or ""
        url = r.get("url") or ""
        snippet = r.get("content") or ""
        if title and url:
            items.append((title, url, snippet))
    return items


def _brave_search(query: str, max_results: int, key: str) -> list[tuple[str, str, str]]:
    """Call the Brave Search API. Raises on HTTP error."""
    with _api_client() as client:
        resp = client.get(
            _BRAVE_URL,
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    items: list[tuple[str, str, str]] = []
    for r in data.get("web", {}).get("results", [])[:max_results]:
        title = r.get("title") or ""
        url = r.get("url") or ""
        snippet = r.get("description") or ""
        if title and url:
            items.append((title, url, snippet))
    return items


def _exa_search(query: str, max_results: int, key: str) -> list[tuple[str, str, str]]:
    """Call the Exa Search API. Raises on HTTP error."""
    with _api_client() as client:
        resp = client.post(
            _EXA_URL,
            json={"query": query, "numResults": max_results},
            headers={"x-api-key": key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    items: list[tuple[str, str, str]] = []
    for r in data.get("results", [])[:max_results]:
        title = r.get("title") or ""
        url = r.get("url") or ""
        highlights = r.get("highlights") or []
        snippet = highlights[0] if highlights else (r.get("text") or "")
        if title and url:
            items.append((title, url, snippet))
    return items


def _run_provider(provider: str, query: str, max_results: int, key: str) -> str:
    """Dispatch to the named provider client and format results.

    Any exception from the provider is caught and returned as a tool-error
    string so the agent always gets a readable message.
    """
    try:
        if provider == "tavily":
            items = _tavily_search(query, max_results, key)
        elif provider == "brave":
            items = _brave_search(query, max_results, key)
        elif provider == "exa":
            items = _exa_search(query, max_results, key)
        else:
            return f"web_search failed: unknown provider {provider!r}"
        return _format_results(query, items)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # httpx.HTTPStatusError is a subclass of httpx.HTTPError — no need to list it.
        return f"web_search failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"web_search failed: {exc}"


# ---------------------------------------------------------------------------
# DuckDuckGo scraper (keyless fallback)
# ---------------------------------------------------------------------------

_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'(?:class="result__snippet"[^>]*>(.*?)</a>)?',
    re.DOTALL,
)


def _strip_html(raw: str) -> str:
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _ddg_search(query: str, max_results: int) -> str:
    """DuckDuckGo HTML scraper — keyless fallback.

    Routes through the SSRF guard (IP-pinned, redirect-validated) so the agent
    can't be tricked into reaching private addresses via a DDG redirect.
    """
    from urllib.parse import urlencode

    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return f"web_search blocked: unsupported scheme {parsed.scheme!r}"
            _ips, reason = _check_host(parsed.hostname or "")
            if reason:
                return f"web_search blocked: {reason}"
            raw = _fetch_raw(url)
            if raw.status_code in _REDIRECT_CODES:
                location = raw.headers.get("location") or raw.headers.get("Location")
                if not location:
                    break
                url = urljoin(url, location)
                continue
            if raw.status_code >= 400:
                return f"web_search failed: HTTP {raw.status_code}"
            break
        else:
            return "web_search failed: too many redirects"
    except (httpx.HTTPError, ValueError) as exc:
        return f"web_search failed: {exc}"

    items: list[tuple[str, str, str]] = []
    for m in _RESULT_RE.finditer(raw.text):
        href = m.group(1)
        title = _strip_html(m.group(2) or "")
        snippet = _strip_html(m.group(3) or "")
        # DuckDuckGo wraps target URLs in a redirect; unwrap uddg= param.
        um = re.search(r"uddg=([^&]+)", href)
        resolved_url = unquote(um.group(1)) if um else href
        if title and resolved_url.startswith("http"):
            items.append((title, resolved_url, snippet))
        if len(items) >= max_results:
            break

    return _format_results(query, items)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return the top results (title, URL, snippet).

    Use this to find current information. Follow up with web_fetch on a URL to
    read a page in full.
    """
    cfg = _active_config

    if cfg is None:
        return _ddg_search(query, max_results)

    provider = cfg.search.provider.value  # "auto" | "duckduckgo" | "tavily" | "brave" | "exa"

    if provider == "duckduckgo":
        return _ddg_search(query, max_results)

    if provider in ("tavily", "brave", "exa"):
        key = _resolve_provider_key(provider, cfg)
        if key is None:
            return (
                f"web_search: provider {provider!r} selected but no API key resolved "
                f"(set search.api_key or ${{{PROVIDER_ENV[provider]}}})"
            )
        return _run_provider(provider, query, max_results, key)

    # provider == "auto": try tavily → brave → exa, fallback to DDG
    for p in ("tavily", "brave", "exa"):
        key = _resolve_provider_key(p, cfg)
        if key:
            return _run_provider(p, query, max_results, key)
    return _ddg_search(query, max_results)


@tool
def web_fetch(url: str, max_chars: int = 6000) -> str:
    """Fetch a URL and return its readable text content (HTML stripped).

    Refuses private/loopback/link-local targets (SSRF guard) — re-checked on
    every redirect hop — and caps the download size. Set
    ``JARN_WEB_FETCH_ALLOW_HOSTS`` to allow specific internal hosts explicitly.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        for _ in range(_MAX_REDIRECTS + 1):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return f"web_fetch blocked: unsupported scheme {parsed.scheme!r}"
            _ips, reason = _check_host(parsed.hostname or "")
            if reason:
                return f"web_fetch blocked: {reason}"
            raw = _fetch_raw(url)
            if raw.status_code in _REDIRECT_CODES:
                location = raw.headers.get("location") or raw.headers.get("Location")
                if not location:
                    break
                url = urljoin(url, location)
                continue
            if raw.status_code >= 400:
                return f"web_fetch failed: HTTP {raw.status_code}"
            break
        else:
            return "web_fetch failed: too many redirects"
    except (httpx.HTTPError, ValueError) as exc:
        return f"web_fetch failed: {exc}"

    ctype = raw.headers.get("content-type", "")
    body = raw.text
    if "html" in ctype or body.lstrip().startswith("<"):
        # Drop script/style blocks before stripping tags.
        body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = _strip_html(body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n…[truncated, {len(body)} chars total]"
    return body or "(empty response)"


def build_web_tools(config: Config | None = None) -> list:
    """Return the built-in web tools, wiring in the active config.

    The config is stored in the module-level ``_active_config`` so the
    ``@tool``-decorated ``web_search`` can read it at call time without
    changing its LangChain-visible signature (``query``, ``max_results``).
    This avoids both a closure (which would break direct-import tests) and
    any framework-visible signature change.
    """
    global _active_config
    _active_config = config
    return [web_search, web_fetch]
