"""D1：抓取侧 SSRF 防护（协议/端口/私网地址/重定向/响应大小）。"""
import pytest

from fetch import extractor


def test_private_and_special_ips_are_blocked():
    for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0"):
        assert extractor._is_blocked_ip(ip) is True


def test_public_ip_is_allowed():
    assert extractor._is_blocked_ip("93.184.216.34") is False


def test_scheme_and_port_are_restricted():
    for url in ("file:///etc/passwd", "ftp://example.com/x", "http://example.com:8080"):
        with pytest.raises(extractor.UnsafeURLError):
            extractor.validate_fetch_url(url)


def test_public_host_is_accepted(monkeypatch):
    monkeypatch.setattr(extractor, "_resolved_addresses", lambda host, port: ["93.184.216.34"])
    assert extractor.validate_fetch_url("https://example.com/a") == "https://example.com/a"


def test_host_resolving_to_private_is_rejected(monkeypatch):
    monkeypatch.setattr(extractor, "_resolved_addresses", lambda host, port: ["127.0.0.1"])
    with pytest.raises(extractor.UnsafeURLError):
        extractor.validate_fetch_url("https://example.com")


def test_redirect_to_private_is_blocked(monkeypatch):
    calls = []

    class FakeResp:
        status_code = 302
        headers = {"location": "http://127.0.0.1/secret"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr(extractor.httpx, "stream", fake_stream)
    monkeypatch.setattr(
        extractor,
        "_resolved_addresses",
        lambda host, port: ["93.184.216.34"] if host == "example.com" else ["127.0.0.1"],
    )

    with pytest.raises(extractor.UnsafeURLError):
        extractor._fetch_text("https://example.com/start")

    assert calls == ["https://example.com/start"]


def test_response_size_is_capped(monkeypatch):
    class FakeResp:
        status_code = 200
        headers = {}
        charset_encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"a" * 1000

    monkeypatch.setattr(extractor.httpx, "stream", lambda *args, **kwargs: FakeResp())
    monkeypatch.setattr(extractor, "_resolved_addresses", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(extractor.config, "MAX_FETCH_BYTES", 100)

    assert len(extractor._fetch_text("https://example.com")) == 100


def test_fetch_page_blocks_loopback():
    result = extractor.fetch_page("http://127.0.0.1/")

    assert result.ok is False
    assert "UnsafeURL" in result.error
