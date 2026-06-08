"""web_fetch SSRF hardening — IP pinning closes the DNS-rebinding TOCTOU."""

from __future__ import annotations

from unittest.mock import MagicMock

from jarn.agent import web_tools


def test_pinned_request_uses_resolved_ip(monkeypatch):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda host: ["93.184.216.34"])
    connect_url, headers, extensions = web_tools._pinned_request(
        "https://example.com/path"
    )
    assert connect_url.startswith("https://93.184.216.34/")
    assert headers["Host"] == "example.com"
    assert extensions["sni_hostname"] == "example.com"


def test_pinned_request_brackets_ipv6(monkeypatch):
    """IPv6 pinned IPs must be bracketed or httpx mis-parses colons as a port."""
    ipv6 = "2606:4700:3036::6815:1eda"
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda host: [ipv6])
    connect_url, headers, extensions = web_tools._pinned_request(
        "https://www.thairath.co.th/money/investment/gold"
    )
    assert connect_url.startswith(f"https://[{ipv6}]/")
    assert headers["Host"] == "www.thairath.co.th"
    assert extensions["sni_hostname"] == "www.thairath.co.th"


def test_fetch_raw_pins_connect(monkeypatch):
    seen: dict = {}

    def _fake_stream(self, method, url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        seen["extensions"] = kwargs.get("extensions")
        resp = MagicMock()
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda *a: None
        resp.encoding = "utf-8"
        resp.status_code = 200
        resp.headers = {"content-type": "text/plain"}
        resp.iter_bytes = lambda: iter([b"ok"])
        return resp

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        stream = _fake_stream

    monkeypatch.setattr(web_tools.httpx, "Client", _FakeClient)
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda host: ["93.184.216.34"])
    raw = web_tools._fetch_raw("https://example.com/")
    assert raw.text == "ok"
    assert seen["url"].startswith("https://93.184.216.34/")
    assert seen["headers"]["Host"] == "example.com"
    assert seen["extensions"]["sni_hostname"] == "example.com"
