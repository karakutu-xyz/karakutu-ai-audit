"""Shared result-mapping for the real search providers.

`DuckDuckGoSearchProvider` and `SearXNGSearchProvider` use different transports and different
raw-hit key names, but everything downstream of the raw hits is identical: map each hit to a
`SearchResult`, drop any whose URL is not an absolute http(s) URL, and cap at `max_results`.

That malformed-URL drop is load-bearing. Under rate limiting `ddgs` rotates to a fallback
backend that emits junk — observed: a relative tracking redirect
("/clev?event=StartpageResultClick..."). SearXNG can surface the same kind of relative junk.
Such a hit cannot be fetched by stage 3, so it is not a lead; both providers funnel through
here so the drop happens in exactly one place.
"""
import logging
from typing import Any, Callable, Iterable, List, Tuple

from src.search.base import SearchResult

logger = logging.getLogger(__name__)

# (url, title, snippet) pulled out of one backend-specific raw hit.
HitExtractor = Callable[[Any], Tuple[Any, str, str]]


def results_from_hits(
    raw_hits: Iterable[Any],
    extract: HitExtractor,
    *,
    query: str,
    provider: str,
    max_results: int,
) -> List[SearchResult]:
    """Map raw hits to `SearchResult`s, dropping non-http(s) URLs, capped at `max_results`."""
    results: List[SearchResult] = []
    for raw in raw_hits:
        url, title, snippet = extract(raw)
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            logger.warning("Discarding malformed %s hit for %r: %r", provider, query, url)
            continue
        results.append(SearchResult(url=url, title=title, snippet=snippet))
        if len(results) >= max_results:
            break
    return results
