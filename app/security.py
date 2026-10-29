"""Web 访问控制：Bearer token + 本机回环兜底 + 每分钟限流。

不引入 cookie/session：token 走 Authorization 头，浏览器不会跨站自动携带，
因此不需要额外的 CSRF token。未配置 APP_AUTH_TOKEN 时只允许回环地址，
既能保持本地开发体验，又避免"部署到公网后裸奔"。
"""
import hashlib
import hmac
import logging
import threading
import time

from fastapi import HTTPException, Request

import config

try:
    import redis
except ImportError:  # pragma: no cover - 未安装时回退本地
    redis = None

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
logger = logging.getLogger("deep_research.security")


def _client_host(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def _client_path(request: Request) -> str:
    url = getattr(request, "url", None)
    return getattr(url, "path", "-") or "-"


def configured_tokens() -> list[str]:
    raw = f"{config.APP_AUTH_TOKEN},{config.APP_AUTH_TOKENS}"
    return [token.strip() for token in raw.split(",") if token.strip()]


def require_auth(request: Request) -> None:
    tokens = configured_tokens()
    if tokens:
        supplied = request.headers.get("authorization", "")
        for token in tokens:
            if hmac.compare_digest(supplied, f"Bearer {token}"):
                token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
                request.state.token_id = token_id
                return
        logger.warning(
            "auth_failed",
            extra={"client": _client_host(request), "path": _client_path(request)},
        )
        raise HTTPException(
            status_code=401,
            detail="缺少或错误的访问令牌（Authorization: Bearer <APP_AUTH_TOKEN>）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if _client_host(request) in _LOOPBACK_HOSTS:
        return
    logger.warning(
        "auth_denied_no_token",
        extra={"client": _client_host(request), "path": _client_path(request)},
    )
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
            logger.warning("rate_limited", extra={"client": key, "retry_after_s": retry_after})
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁（每分钟最多 {self.limit} 次），请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )


class RedisRateLimiter:
    """Redis 固定窗口限流；Redis 故障时回退进程内限流并留 warning。"""

    def __init__(self, limit_per_minute: int, url: str) -> None:
        self.limit = max(1, int(limit_per_minute))
        self.url = url
        self._fallback = RateLimiter(self.limit)
        self._client = (
            redis.from_url(url, decode_responses=True, socket_timeout=1.0)
            if redis is not None
            else None
        )

    def check(self, key: str) -> None:
        if self._client is None:
            self._fallback.check(key)
            return
        now = time.time()
        window = int(now // 60)
        redis_key = f"dr:rate:{window}:{key}"
        try:
            pipe = self._client.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, 65)
            count = int(pipe.execute()[0])
        except Exception as exc:
            logger.warning("redis_rate_limit_failed", extra={"client": key})
            self._fallback.check(key)
            return
        if count > self.limit:
            retry_after = max(1, 60 - int(now % 60))
            logger.warning("rate_limited", extra={"client": key, "retry_after_s": retry_after})
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁（每分钟最多 {self.limit} 次），请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )


_limiter = None


def get_limiter():
    global _limiter
    signature = (config.APP_RATE_LIMIT_PER_MINUTE, config.REDIS_URL.strip())
    if _limiter is None or getattr(_limiter, "_signature", None) != signature:
        if config.REDIS_URL.strip() and redis is not None:
            _limiter = RedisRateLimiter(config.APP_RATE_LIMIT_PER_MINUTE, config.REDIS_URL)
        else:
            _limiter = RateLimiter(config.APP_RATE_LIMIT_PER_MINUTE)
        _limiter._signature = signature
    return _limiter


def enforce_rate_limit(request: Request) -> None:
    token_id = getattr(request.state, "token_id", None)
    key = f"token:{token_id}" if token_id else (_client_host(request) or "unknown")
    get_limiter().check(key)


def reset_limiter() -> None:
    global _limiter
    _limiter = None
