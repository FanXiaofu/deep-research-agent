from search.base import SearchProvider, SearchResult
from search.tavily import TavilyProvider
from search.duckduckgo import DuckDuckGoProvider

_PROVIDERS = {
    "tavily": TavilyProvider,
    "duckduckgo": DuckDuckGoProvider,
}


def get_search_provider() -> SearchProvider:
    cls = _PROVIDERS.get(config_name := __import__("config").SEARCH_PROVIDER)
    if cls is None:
        raise ValueError(f"未知 SEARCH_PROVIDER: {config_name}，可选: {list(_PROVIDERS)}")
    return cls()


__all__ = ["SearchProvider", "SearchResult", "get_search_provider"]
