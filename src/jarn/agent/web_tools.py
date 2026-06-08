"""Built-in web tools: ``web_search`` and ``web_fetch``.

These give the agent real read-only internet access (the "Web search + fetch"
v1 capability). They are dependency-light (httpx + stdlib regex; no bs4) and
fail gracefully with a readable message rather than raising, so a flaky network
never crashes a turn.
"""

from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import httpx
from langchain_core.tools import tool

_UA = "Mozilla/5.0 (compatible; JARN/0.1; +https://github.com/chayapats/jarn)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")

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


def _check_host(host: str) -> str | None:
    """Return a block *reason*, or ``None`` if the host is safe to fetch.

    Blocks loopback / private / link-local / reserved targets (so the agent
    can't reach localhost, the LAN, or the cloud-metadata endpoint), unless the
    host is on the explicit allowlist. Both literal IPs and DNS names (every
    resolved address) are checked.

    Callers that fetch must pin the validated address at connect-time (see
    :func:`_fetch_raw`) so a hostile DNS name cannot rebind between check and
    connect.
    """
    if not host:
        return "missing host"
    if host.lower() in _allowlisted_hosts():
        return None
    try:
        ipaddress.ip_address(host)
        ips = [host]                       # literal IP — no DNS needed
    except ValueError:
        try:
            ips = _resolve_ips(host)
        except OSError:
            return f"could not resolve host {host!r}"
    blocked = [ip for ip in ips if _ip_is_blocked(ip)]
    if blocked:
        return f"refusing to reach a private/loopback address ({blocked[0]})"
    return None


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
    reason = _check_host(hostname)
    if reason:
        raise ValueError(reason)

    try:
        ipaddress.ip_address(hostname)
        pinned_ip = hostname
    except ValueError:
        ips = _resolve_ips(hostname)
        blocked = [ip for ip in ips if _ip_is_blocked(ip)]
        if blocked:
            raise ValueError(
                f"refusing to reach a private/loopback address ({blocked[0]})"
            ) from None
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
_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'(?:class="result__snippet"[^>]*>(.*?)</a>)?',
    re.DOTALL,
)


def _strip_html(raw: str) -> str:
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return the top results (title, URL, snippet).

    Use this to find current information. Follow up with web_fetch on a URL to
    read a page in full.
    """
    try:
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": _UA},
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return f"web_search failed: {exc}"

    results: list[str] = []
    for m in _RESULT_RE.finditer(resp.text):
        href, title, snippet = m.group(1), _strip_html(m.group(2) or ""), _strip_html(m.group(3) or "")
        # DuckDuckGo wraps target URLs in a redirect; unwrap uddg= param.
        um = re.search(r"uddg=([^&]+)", href)
        url = unquote(um.group(1)) if um else href
        if title and url.startswith("http"):
            line = f"- {title}\n  {url}"
            if snippet:
                line += f"\n  {snippet}"
            results.append(line)
        if len(results) >= max_results:
            break

    if not results:
        return f"No results for {query!r} (or the search page format changed)."
    return f"Top results for {query!r}:\n\n" + "\n".join(results)


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
            reason = _check_host(parsed.hostname or "")
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


def build_web_tools() -> list:
    """Return the built-in web tools to hand to the agent."""
    return [web_search, web_fetch]
