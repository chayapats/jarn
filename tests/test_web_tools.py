"""web_search / web_fetch tool tests (httpx mocked — no real network)."""

from __future__ import annotations

import httpx

from jarn.agent import web_tools
from jarn.agent.session import _unpack_stream_item
from jarn.agent.web_tools import build_web_tools, web_fetch, web_search


class _Resp:
    def __init__(self, text, ctype="text/html"):
        self.text = text
        self.headers = {"content-type": ctype}

    def raise_for_status(self):
        pass


_DDG_HTML = """
<div class="result">
  <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fgold.example%2Fprice">
    Gold price today</a>
  <a class="result__snippet">Spot gold is $2,345/oz.</a>
</div>
"""


def test_web_search_parses(monkeypatch):
    # web_search now routes through the SSRF-guarded, IP-pinned _fetch_raw path
    # (same as web_fetch), so stub DNS + _fetch_raw rather than httpx.get.
    _public_dns(monkeypatch)
    monkeypatch.setattr(web_tools, "_fetch_raw", lambda url, **k: _raw(_DDG_HTML))
    out = web_search.invoke({"query": "gold price"})
    assert "Gold price today" in out
    assert "https://gold.example/price" in out


def test_web_search_network_error(monkeypatch):
    _public_dns(monkeypatch)

    def boom(*a, **k):
        raise httpx.ConnectError("no net")
    monkeypatch.setattr(web_tools, "_fetch_raw", boom)
    out = web_search.invoke({"query": "x"})
    assert "web_search failed" in out


def test_web_search_blocks_private_redirect(monkeypatch):
    """A search-endpoint redirect to a private address is refused (SSRF guard)."""
    monkeypatch.setattr(
        web_tools, "_resolve_ips",
        lambda host: ["10.0.0.5"] if host == "evil.internal" else ["93.184.216.34"],
    )
    monkeypatch.setattr(
        web_tools, "_fetch_raw",
        lambda url, **k: _raw("", status=302, headers={"location": "http://evil.internal/x"}),
    )
    out = web_search.invoke({"query": "x"})
    assert "web_search blocked" in out


def _public_dns(monkeypatch):
    """Make every hostname resolve to a public IP so the SSRF guard allows it."""
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda host: ["93.184.216.34"])


def _raw(text, status=200, headers=None):
    return web_tools._RawResponse(status, headers or {"content-type": "text/html"}, text)


def test_web_fetch_strips_html(monkeypatch):
    _public_dns(monkeypatch)
    html = "<html><body><script>ignore()</script><h1>Hi</h1><p>World</p></body></html>"
    monkeypatch.setattr(web_tools, "_fetch_raw", lambda url, **k: _raw(html))
    out = web_fetch.invoke({"url": "example.com"})
    assert "Hi" in out and "World" in out
    assert "ignore()" not in out


def test_web_fetch_error(monkeypatch):
    _public_dns(monkeypatch)

    def boom(url, **k):
        raise httpx.HTTPError("bad")

    monkeypatch.setattr(web_tools, "_fetch_raw", boom)
    assert "web_fetch failed" in web_fetch.invoke({"url": "https://x"})


def test_web_fetch_blocks_loopback():
    # Literal loopback IP — refused before any network call (no DNS needed).
    assert "blocked" in web_fetch.invoke({"url": "http://127.0.0.1/secret"})


def test_web_fetch_blocks_cloud_metadata():
    # 169.254.169.254 (link-local) is the cloud metadata endpoint.
    assert "blocked" in web_fetch.invoke({"url": "http://169.254.169.254/latest/meta-data/"})


def test_web_fetch_blocks_private_dns(monkeypatch):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda host: ["10.0.0.5"])
    assert "blocked" in web_fetch.invoke({"url": "http://internal.corp/"})


def test_web_fetch_blocks_cgnat():
    # 100.64.0.0/10 is RFC6598 carrier-grade NAT — not "private" per ipaddress.
    assert "blocked" in web_fetch.invoke({"url": "http://100.64.0.1/"})


def test_web_fetch_blocks_redirect_to_non_http_scheme(monkeypatch):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda host: ["93.184.216.34"])
    # A redirect to file:// (or any non-http scheme) is rejected explicitly.
    monkeypatch.setattr(
        web_tools, "_fetch_raw",
        lambda url, **k: _raw("", status=302, headers={"location": "file:///etc/passwd"}),
    )
    assert "blocked" in web_fetch.invoke({"url": "http://example.com/"})


def test_web_fetch_allowlist_overrides(monkeypatch):
    monkeypatch.setenv("JARN_WEB_FETCH_ALLOW_HOSTS", "internal.corp")
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda host: ["10.0.0.5"])
    monkeypatch.setattr(web_tools, "_fetch_raw", lambda url, **k: _raw("<p>ok</p>"))
    assert "ok" in web_fetch.invoke({"url": "http://internal.corp/"})


def test_web_fetch_re_checks_redirect_target(monkeypatch):
    def resolve(host):
        return {"example.com": ["93.184.216.34"], "internal.corp": ["10.0.0.5"]}[host]

    monkeypatch.setattr(web_tools, "_resolve_ips", resolve)
    # First (public) hop 302-redirects to an internal host → must be refused.
    monkeypatch.setattr(
        web_tools, "_fetch_raw",
        lambda url, **k: _raw("", status=302, headers={"location": "http://internal.corp/x"}),
    )
    assert "blocked" in web_fetch.invoke({"url": "http://example.com/"})


def test_fetch_raw_caps_bytes(monkeypatch):
    class _FakeStream:
        status_code = 200
        headers = {"content-type": "text/plain"}
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            for _ in range(10):
                yield b"x" * 500_000  # 5 MB total

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            return _FakeStream()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda host: ["93.184.216.34"])
    raw = web_tools._fetch_raw("http://example.com", max_bytes=1_000_000)
    assert len(raw.text) == 1_000_000  # stopped at the cap, didn't load 5 MB


def test_pinned_request_resolves_dns_once_no_rebinding(monkeypatch):
    """_pinned_request must resolve DNS exactly once and pin the checked IP.

    Regression for the DNS-rebinding TOCTOU: a hostile short-TTL record that
    serves a public IP on the first lookup and 127.0.0.1 on a second lookup must
    not slip through. We make _resolve_ips return a *public* IP first and a
    *loopback* IP on any subsequent call; the single-resolution design means the
    second (malicious) answer is never consulted, so the connect pins the public
    IP that was validated.
    """
    calls = {"n": 0}

    def rebinding_resolve(host):
        calls["n"] += 1
        # First lookup: safe public IP. Any later lookup: loopback (attack).
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    monkeypatch.setattr(web_tools, "_resolve_ips", rebinding_resolve)
    connect_url, _headers, _ext = web_tools._pinned_request("http://example.com/")
    assert calls["n"] == 1, "DNS must be resolved exactly once (no rebinding window)"
    # The connect URL must pin the validated public IP, never the loopback.
    assert "93.184.216.34" in connect_url
    assert "127.0.0.1" not in connect_url


def test_build_web_tools():
    tools = build_web_tools()
    names = {t.name for t in tools}
    assert names == {"web_search", "web_fetch"}


def test_unpack_stream_item():
    # subgraphs=True items carry the namespace path as the first element; it is
    # now threaded through (for per-call cost attribution), not discarded.
    assert _unpack_stream_item((("ns",), "messages", {"a": 1})) == (("ns",), "messages", {"a": 1})
    # Without subgraphs the namespace defaults to an empty tuple.
    assert _unpack_stream_item(("updates", {"b": 2})) == ((), "updates", {"b": 2})
    assert _unpack_stream_item("nonsense") == ((), None, None)
