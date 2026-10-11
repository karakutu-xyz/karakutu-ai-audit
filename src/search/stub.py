from typing import List, Optional
from src.search.base import BaseSearchProvider, SearchResult


class StubSearchProvider(BaseSearchProvider):
    """Offline stub returning canned results. Kept deliberately as the default
    (`WEB_SEARCH_PROVIDER=stub`) so tests and offline runs never hit the network.
    For real results use `WEB_SEARCH_PROVIDER=duckduckgo` (`src/search/duckduckgo.py`)."""

    async def search(self, query: str, max_results: int = 5,
                     *, language: Optional[str] = None) -> List[SearchResult]:
        # `language` is accepted for interface compatibility and ignored: canned results.
        return [
            SearchResult(url="https://example.com/article-1", title="Stub result 1",
                         snippet="Placeholder snippet for query: " + query),
            SearchResult(url="https://example.com/article-2", title="Stub result 2",
                         snippet="Second placeholder snippet."),
            SearchResult(url="https://example.com/article-3", title="Stub result 3",
                         snippet="Third placeholder snippet."),
        ]
