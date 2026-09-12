"""DuckDuckGo 免费兜底搜索（无需 key，国内网络可能不可达）。"""
from search.base import SearchProvider, SearchResult


class DuckDuckGoProvider(SearchProvider):
    name = "duckduckgo"

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise RuntimeError("未安装 ddgs：pip install ddgs") from exc
        with DDGS() as ddgs:
            rows = list(ddgs.text(query, max_results=max_results))
        return [
            SearchResult(
                title=row.get("title", ""),
                url=row.get("href", "") or row.get("url", ""),
                snippet=row.get("body", ""),
            )
            for row in rows
        ]
