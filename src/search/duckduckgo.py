"""Real web search via the `ddgs` DuckDuckGo client."""
import logging
from typing import Any, Dict, List, Optional

from anyio import to_thread
from ddgs import DDGS

from src.search._common import results_from_hits
from src.search.base import BaseSearchProvider, SearchResult

logger = logging.getLogger(__name__)


class DuckDuckGoSearchProvider(BaseSearchProvider):
    """Real web search via `ddgs`.

    The underlying client is synchronous, so the call is pushed off the event
    loop with `anyio.to_thread.run_sync`. Any backend failure (rate limit,
    network, parse) degrades to an empty result list: the pipeline reads "no
    hits" as INCONCLUSIVE, never as evidence of censorship.
    """

    async def search(self, query: str, max_results: int = 5,
                     *, language: Optional[str] = None) -> List[SearchResult]:
        # `language` is accepted for interface compatibility and ignored: ddgs region
        # codes are unreliable on the degraded fallback backends this client rotates to.
        try:
            raw = await to_thread.run_sync(self._search_sync, query, max_results)
        except Exception as e:  # rate-limit / network / parse — degrade, never propagate
            logger.warning(f"DuckDuckGo search failed for query '{query}': {e}")
            return []

        # ddgs 9.x already honours `max_results`; slicing here preserves the historical
        # truncation semantics (keep the valid hits among the first `max_results`).
        return results_from_hits(
            raw[:max_results],
            lambda hit: (hit.get("href"), hit.get("title", ""), hit.get("body", "")),
            query=query,
            provider="DuckDuckGo",
            max_results=max_results,
        )

    @staticmethod
    def _search_sync(query: str, max_results: int) -> List[Dict[str, Any]]:
        # ddgs 9.x: `text()` returns a list of dicts keyed title/href/body and
        # already honours `max_results`. Materialize here so all network I/O
        # happens inside the worker thread.
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
