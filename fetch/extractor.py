"""网页抓取与正文抽取（httpx + BeautifulSoup）。

安全边界（URL 来自搜索结果，属于不可信输入）：
- 只允许 http/https、80/443；
- 目标主机解析出的所有 IP 都不能是私网/回环/链路本地/保留/组播地址；
- 手动跟随重定向（每一跳都重新校验），最多 FETCH_MAX_REDIRECTS 次；
- 响应最多读取 MAX_FETCH_BYTES，超出截断。

说明：校验与实际连接之间仍存在 DNS rebinding 的残余窗口；要彻底消除需要把
解析结果固定到连接层（自定义 transport），这里选择先挡住常见的内网访问路径。
"""
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

import config

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

# 噪音标签：对研究正文没有贡献，直接剔除
_NOISE_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript")
_ALLOWED_SCHEMES = {"http", "https"}
_ALLOWED_PORTS = {80, 443}
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


class UnsafeURLError(ValueError):
    """目标 URL 不满足抓取安全边界。"""


@dataclass
class FetchResult:
    url: str
    title: str = ""
    text: str = ""
    ok: bool = False
    error: str = ""

    def as_prompt_block(self, max_chars: int) -> str:
        if not self.ok or not self.text:
            return f"[网页抓取失败] {self.url} ({self.error})"
        return f"{self.text[:max_chars]}"


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolved_addresses(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def validate_fetch_url(url: str) -> str:
    """校验 URL 的协议、端口与解析结果；不安全时抛 UnsafeURLError。"""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"只允许 http/https，收到 {parsed.scheme or '(空)'}")
    if not parsed.hostname:
        raise UnsafeURLError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URL 不允许携带 userinfo")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError(f"端口非法：{exc}") from exc
    if port not in _ALLOWED_PORTS:
        raise UnsafeURLError(f"只允许 80/443 端口，收到 {port}")

    try:
        addresses = _resolved_addresses(parsed.hostname, port)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS 解析失败：{exc}") from exc
    if not addresses:
        raise UnsafeURLError("DNS 没有返回地址")
    blocked = [ip for ip in addresses if _is_blocked_ip(ip)]
    if blocked:
        raise UnsafeURLError(f"目标解析到内网/保留地址：{blocked[0]}")
    return url


def _read_limited(resp: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        if not chunk:
            continue
        remaining = config.MAX_FETCH_BYTES - total
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        total += min(len(chunk), remaining)
        if total >= config.MAX_FETCH_BYTES:
            break
    return b"".join(chunks)


def _fetch_text(url: str) -> str:
    current = url
    for _ in range(config.FETCH_MAX_REDIRECTS + 1):
        validate_fetch_url(current)
        with httpx.stream(
            "GET",
            current,
            headers=HEADERS,
            timeout=config.FETCH_TIMEOUT,
            follow_redirects=False,
        ) as resp:
            if resp.status_code in _REDIRECT_STATUS:
                location = resp.headers.get("location")
                if not location:
                    raise UnsafeURLError(f"重定向缺少 Location：{current}")
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            body = _read_limited(resp)
            charset = resp.charset_encoding or "utf-8"
            return body.decode(charset, errors="replace")
    raise UnsafeURLError(f"重定向超过 {config.FETCH_MAX_REDIRECTS} 次")


def fetch_page(url: str) -> FetchResult:
    try:
        html = _fetch_text(url)
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(_NOISE_TAGS):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n".join(p for p in paragraphs if len(p) > 40)
        return FetchResult(url=url, title=title, text=text[: config.MAX_PAGE_CHARS], ok=bool(text))
    except UnsafeURLError as exc:
        return FetchResult(url=url, error=f"UnsafeURL: {exc}")
    except Exception as exc:  # 网络错误/超时/编码问题都归为抓取失败
        return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")
