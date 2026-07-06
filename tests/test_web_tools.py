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


# ---------------------------------------------------------------------------
# P5.A — web_search richer inline summary
# ---------------------------------------------------------------------------

def _make_search_output(*urls: str) -> str:
    """Build a realistic web_search output string for the given URLs."""
    lines = ["Top results for 'test query':", ""]
    for i, url in enumerate(urls, 1):
        lines.append(f"- Result {i}")
        lines.append(f"  {url}")
        lines.append(f"  Some snippet for result {i}.")
        lines.append("")
    return "\n".join(lines)


def test_web_search_summary_names_hosts():
    """_tool_summary for web_search shows count and source hosts."""
    from jarn.agent.session import _tool_summary

    content = _make_search_output(
        "https://example.com/page",
        "https://wikipedia.org/wiki/X",
        "https://docs.python.org/3/",
    )
    summary = _tool_summary(content, "web_search")
    assert summary.startswith("🔍 3 results")
    assert "example.com" in summary
    assert "wikipedia.org" in summary


def test_web_search_summary_strips_www():
    """www. prefix is stripped from host names for compactness."""
    from jarn.agent.session import _tool_summary

    content = _make_search_output("https://www.bbc.co.uk/news")
    summary = _tool_summary(content, "web_search")
    assert "bbc.co.uk" in summary
    assert "www." not in summary


def test_web_search_summary_truncates_at_three_hosts():
    """Only 3 hosts are shown; a trailing '…' indicates more."""
    from jarn.agent.session import _tool_summary

    content = _make_search_output(
        "https://a.com/",
        "https://b.com/",
        "https://c.com/",
        "https://d.com/",
        "https://e.com/",
    )
    summary = _tool_summary(content, "web_search")
    assert "a.com" in summary
    assert "b.com" in summary
    assert "c.com" in summary
    assert "d.com" not in summary
    assert "…" in summary


def test_web_search_summary_single_result():
    """Singular 'result' (not 'results') when count is 1."""
    from jarn.agent.session import _tool_summary

    content = _make_search_output("https://sole.example.com/only")
    summary = _tool_summary(content, "web_search")
    assert "1 result" in summary
    assert "1 results" not in summary


def test_tool_summary_non_web_search_unchanged():
    """Non-web_search tool names still get the generic summary."""
    from jarn.agent.session import _tool_summary

    multi_line = "line1\nline2\nline3"
    assert _tool_summary(multi_line, "bash") == "3 lines"
    assert _tool_summary(multi_line) == "3 lines"  # default tool_name=""


# ---------------------------------------------------------------------------
# T-3-4 — Pluggable web-search providers
# ---------------------------------------------------------------------------

def _make_search_cfg(provider: str = "auto", api_key: str = ""):
    """Build a minimal Config with specific search settings for testing."""
    from jarn.config.schema import Config, SearchConfig, SearchProviderType
    cfg = Config()
    cfg.search = SearchConfig(provider=SearchProviderType(provider), api_key=api_key)
    return cfg


def _mock_transport(handler):
    """Return an httpx Client factory using a MockTransport with the given handler."""
    transport = httpx.MockTransport(handler)
    return lambda: httpx.Client(transport=transport)


def test_tavily(monkeypatch):
    """Tavily: POST to api.tavily.com/search with key in body, parse title/url/content."""
    import json as _json

    cfg = _make_search_cfg("tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key-12345")

    def handler(request):
        body = _json.loads(request.content)
        assert body["api_key"] == "tvly-test-key-12345"
        assert str(request.url) == "https://api.tavily.com/search"
        return httpx.Response(200, json={
            "results": [
                {"title": "TavTitle", "url": "https://tav.example/1", "content": "TavSnip"}
            ]
        })

    monkeypatch.setattr(web_tools, "_api_client", _mock_transport(handler))
    monkeypatch.setattr(web_tools, "_active_config", cfg)
    out = web_search.invoke({"query": "tavily test"})
    assert "Top results for 'tavily test'" in out
    assert "TavTitle" in out
    assert "https://tav.example/1" in out
    assert "TavSnip" in out


def test_brave(monkeypatch):
    """Brave: GET api.search.brave.com/res/v1/web/search with X-Subscription-Token."""
    cfg = _make_search_cfg("brave")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key-12345")

    def handler(request):
        assert request.headers.get("X-Subscription-Token") == "brave-test-key-12345"
        assert "api.search.brave.com" in str(request.url)
        return httpx.Response(200, json={
            "web": {
                "results": [
                    {
                        "title": "BraveTitle",
                        "url": "https://brave.example/2",
                        "description": "BraveSnip",
                    }
                ]
            }
        })

    monkeypatch.setattr(web_tools, "_api_client", _mock_transport(handler))
    monkeypatch.setattr(web_tools, "_active_config", cfg)
    out = web_search.invoke({"query": "brave test"})
    assert "Top results for 'brave test'" in out
    assert "BraveTitle" in out
    assert "https://brave.example/2" in out
    assert "BraveSnip" in out


def test_exa(monkeypatch):
    """Exa: POST to api.exa.ai/search with x-api-key header, parse highlights."""
    cfg = _make_search_cfg("exa")
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key-12345")

    def handler(request):
        assert request.headers.get("x-api-key") == "exa-test-key-12345"
        assert "api.exa.ai" in str(request.url)
        return httpx.Response(200, json={
            "results": [
                {
                    "title": "ExaTitle",
                    "url": "https://exa.example/3",
                    "highlights": ["ExaSnip"],
                }
            ]
        })

    monkeypatch.setattr(web_tools, "_api_client", _mock_transport(handler))
    monkeypatch.setattr(web_tools, "_active_config", cfg)
    out = web_search.invoke({"query": "exa test"})
    assert "Top results for 'exa test'" in out
    assert "ExaTitle" in out
    assert "https://exa.example/3" in out
    assert "ExaSnip" in out


def test_auto_selection(monkeypatch):
    """auto: first provider whose key resolves wins; falls back to DDG when none set."""
    cfg = _make_search_cfg("auto")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "brave-auto-key-12345")

    def handler(request):
        assert "api.search.brave.com" in str(request.url), (
            f"expected brave endpoint, got {request.url}"
        )
        return httpx.Response(200, json={
            "web": {
                "results": [
                    {"title": "AutoBrave", "url": "https://auto.example/b", "description": ""}
                ]
            }
        })

    monkeypatch.setattr(web_tools, "_api_client", _mock_transport(handler))
    monkeypatch.setattr(web_tools, "_active_config", cfg)
    out = web_search.invoke({"query": "auto test"})
    assert "AutoBrave" in out


def test_auto_fallback_ddg(monkeypatch):
    """auto with NO provider key resolvable falls back to the keyless DDG scraper.

    The plan's failing-test spec named this branch ("`auto` picks the first
    provider whose key resolves, ELSE DuckDuckGo") but no test exercised it.
    """
    cfg = _make_search_cfg("auto", api_key="")  # search.api_key explicitly empty
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    # No keychain entry for any provider in the test env — auto falls to DDG.

    # DDG routes through the SSRF-guarded, IP-pinned _fetch_raw path.
    _public_dns(monkeypatch)
    seen: list[str] = []

    def _fake_fetch(url, **k):
        seen.append(url)
        return _raw(_DDG_HTML)

    monkeypatch.setattr(web_tools, "_fetch_raw", _fake_fetch)
    monkeypatch.setattr(web_tools, "_active_config", cfg)

    out = web_search.invoke({"query": "gold price"})
    # The DDG scraper ran (its endpoint was hit) and returned formatted results.
    assert any("duckduckgo.com" in u for u in seen), (
        f"auto with no keys must fall back to the DDG scraper; hit {seen}"
    )
    assert "Gold price today" in out
    assert "https://gold.example/price" in out


def test_provider_error_string(monkeypatch):
    """HTTP failure from a provider returns a tool-error string; never raises."""
    cfg = _make_search_cfg("tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key-12345")

    def handler(request):
        return httpx.Response(500, text="Internal Server Error")

    monkeypatch.setattr(web_tools, "_api_client", _mock_transport(handler))
    monkeypatch.setattr(web_tools, "_active_config", cfg)
    out = web_search.invoke({"query": "error test"})
    assert "web_search failed" in out
    assert isinstance(out, str)
    # The resolved API key must never leak into the surfaced error string.
    assert "tvly-test-key-12345" not in out


def test_key_by_reference(monkeypatch):
    """search.api_key as a ${ENV} reference resolves to the real value; key never in output."""
    from jarn.config.schema import Config, SearchConfig, SearchProviderType

    cfg = Config()
    cfg.search = SearchConfig(
        provider=SearchProviderType.TAVILY,
        api_key="${TAVILY_API_KEY}",  # env-var reference, not a literal key
    )
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-ref-resolved-12345")

    import json as _json

    def handler(request):
        body = _json.loads(request.content)
        assert body.get("api_key") == "tvly-ref-resolved-12345", (
            f"key not resolved correctly: {body}"
        )
        return httpx.Response(200, json={
            "results": [
                {"title": "RefTitle", "url": "https://ref.example/", "content": "RefSnip"}
            ]
        })

    monkeypatch.setattr(web_tools, "_api_client", _mock_transport(handler))
    monkeypatch.setattr(web_tools, "_active_config", cfg)
    out = web_search.invoke({"query": "ref key test"})
    assert "RefTitle" in out
    # The resolved key value must never appear in the returned string
    assert "tvly-ref-resolved-12345" not in out
