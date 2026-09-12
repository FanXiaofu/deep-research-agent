"""网页抓取与正文抽取（httpx + BeautifulSoup），失败不抛异常、返回 ok=False。"""
from dataclasses import dataclass, field

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


def fetch_page(url: str) -> FetchResult:
    try:
        resp = httpx.get(
            url, headers=HEADERS, timeout=config.FETCH_TIMEOUT, follow_redirects=True
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(_NOISE_TAGS):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n".join(p for p in paragraphs if len(p) > 40)
        return FetchResult(url=url, title=title, text=text[: config.MAX_PAGE_CHARS], ok=bool(text))
    except Exception as exc:  # 网络错误/超时/编码问题都归为抓取失败
        return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")
