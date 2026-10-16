from typing import Dict, Type
from src.search.base import BaseSearchProvider, SearchResult
from src.search.duckduckgo import DuckDuckGoSearchProvider
from src.search.searxng import SearXNGSearchProvider
from src.search.stub import StubSearchProvider

SEARCH_REGISTRY: Dict[str, Type[BaseSearchProvider]] = {
    "stub": StubSearchProvider,
    "duckduckgo": DuckDuckGoSearchProvider,
    "searxng": SearXNGSearchProvider,
}


def get_search_provider(name: str) -> BaseSearchProvider:
    key = name.lower().strip()
    if key not in SEARCH_REGISTRY:
        available = ", ".join(sorted(SEARCH_REGISTRY.keys()))
        raise ValueError(f"Unknown search provider: '{name}'. Available: {available}")
    return SEARCH_REGISTRY[key]()


__all__ = ["BaseSearchProvider", "SearchResult", "StubSearchProvider",
           "DuckDuckGoSearchProvider", "SearXNGSearchProvider",
           "SEARCH_REGISTRY", "get_search_provider"]
