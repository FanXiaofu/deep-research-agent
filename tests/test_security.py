"""D1：Bearer 认证、回环兜底与限流。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import config
from app import security


def _request(host="127.0.0.1", headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers or {})


def test_token_required_when_configured(monkeypatch):
    monkeypatch.setattr(config, "APP_AUTH_TOKEN", "s3cret")

    with pytest.raises(HTTPException) as missing:
        security.require_auth(_request(headers={}))
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException):
        security.require_auth(_request(headers={"authorization": "Bearer wrong"}))

    security.require_auth(_request(headers={"authorization": "Bearer s3cret"}))


def test_empty_token_allows_loopback_only(monkeypatch):
    monkeypatch.setattr(config, "APP_AUTH_TOKEN", "")

    security.require_auth(_request(host="127.0.0.1"))

    with pytest.raises(HTTPException) as denied:
        security.require_auth(_request(host="203.0.113.10"))
    assert denied.value.status_code == 403


def test_rate_limiter_blocks_after_limit(monkeypatch):
    monkeypatch.setattr(config, "APP_RATE_LIMIT_PER_MINUTE", 1)
    security.reset_limiter()
    limiter = security.get_limiter()

    limiter.check("1.2.3.4")
    with pytest.raises(HTTPException) as blocked:
        limiter.check("1.2.3.4")

    assert blocked.value.status_code == 429
    assert "Retry-After" in blocked.value.headers
    limiter.check("5.6.7.8")  # 其他客户端不受影响
