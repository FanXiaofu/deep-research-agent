"""Web 访问控制：Bearer token + 本机回环兜底 + 每分钟限流。

不引入 cookie/session：token 走 Authorization 头，浏览器不会跨站自动携带，
因此不需要额外的 CSRF token。未配置 APP_AUTH_TOKEN 时只允许回环地址，
既能保持本地开发体验，又避免"部署到公网后裸奔"。
"""
import hmac
import threading
import time

from fastapi import HTTPException, Request

import config

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _client_host(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def require_auth(request: Request) -> None:
    token = config.APP_AUTH_TOKEN.strip()
    if token:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {token}"):
            raise HTTPException(
                status_code=401,
                detail="缺少或错误的访问令牌（Authorization: Bearer <APP_AUTH_TOKEN>）",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return
    if _client_host(request) in _LOOPBACK_HOSTS:
        return
    raise HTTPException(
        status_code=403,
        detail="服务未配置 APP_AUTH_TOKEN，拒绝非本机访问",
    )


class RateLimiter:
    """单进程固定窗口限流；多 worker 部署时应换成 Redis 等共享实现。"""

    def __init__(self, limit_per_minute: int) -> None:
        self.limit = max(1, int(limit_per_minute))
        self._lock = threading.Lock()
        self._window = 0
        self._counts: dict[str, int] = {}

    def check(self, key: str) -> None:
        now = time.time()
        window = int(now // 60)
        with self._lock:
            if window != self._window:
                self._window = window
                self._counts.clear()
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
        if count > self.limit:
            retry_after = max(1, 60 - int(now % 60))
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁（每分钟最多 {self.limit} 次），请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None or _limiter.limit != config.APP_RATE_LIMIT_PER_MINUTE:
        _limiter = RateLimiter(config.APP_RATE_LIMIT_PER_MINUTE)
    return _limiter


def enforce_rate_limit(request: Request) -> None:
    get_limiter().check(_client_host(request) or "unknown")


def reset_limiter() -> None:
    global _limiter
    _limiter = None
