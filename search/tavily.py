"""Tavily 搜索（面向 Agent 设计，免费额度 1000 次/月）。"""
import httpx

import config
from search.base import SearchProvider, SearchResult

API_URL = "https://api.tavily.com/search"


class TavilyProvider(SearchProvider):
    name = "tavily"

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not config.SEARCH_API_KEY:
            raise RuntimeError("SEARCH_API_KEY 未配置（.env 中填写 Tavily key）")
        resp = httpx.post(
            API_URL,
            json={"query": query, "max_results": max_results, "search_depth": "basic"},
            headers={"Authorization": f"Bearer {config.SEARCH_API_KEY}"},
            timeout=20,
        )
        resp.raise_for_status()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
            )
            for item in resp.json().get("results", [])
        ]
