"""搜索提供方统一抽象：换搜索服务只需新增一个子类并注册到 search/__init__.py。"""
from dataclasses import dataclass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class SearchProvider:
    name = "base"

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise NotImplementedError
