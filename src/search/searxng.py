"""Search via a self-hosted SearXNG instance.

Why this exists: `DuckDuckGoSearchProvider` queries one engine, and under rate limiting
`ddgs` rotates to a fallback backend that returns junk (Instagram profiles, academic PDFs).
Consequence — it can fail to return a relocated copy that is plainly live (answering 200 at
its new same-publisher URL). Stage 3 cannot reason about a story the search never surfaces, and it is
why the "not found → flag" rule had to be disabled (see TODO(not-found-flag) in pipeline.py).

SearXNG fans one query out to many engines (Google, Bing, Brave, DuckDuckGo, regional) and
merges the results, so recall no longer rests on a single scraper having a good minute.
It is self-hosted: no API key, no per-query cost, and queries for suppressed articles do not
leave the operator's infrastructure — which keeps this service standalone.
"""
import logging
from typing import Any, Dict, List, Optional

import httpx

from src.config.settings import get_settings
from src.search._common import results_from_hits
from src.search.base import BaseSearchProvider, SearchResult

logger = logging.getLogger(__name__)


class SearXNGSearchProvider(BaseSearchProvider):
    """Real search via a self-hosted SearXNG JSON API.

    Degrades to `[]` on any failure, exactly like the DuckDuckGo provider: an unreachable
    search backend must never 500 the audit, and "no hits" is never evidence of anything
    (`pipeline._decide` turns it into INCONCLUSIVE with an explicit search-quality note).
    """

    async def search(self, query: str, max_results: int = 5,
                     *, language: Optional[str] = None) -> List[SearchResult]:
        settings = get_settings()
        base = settings.SEARXNG_URL.rstrip("/")
        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "categories": "general",
            # The caller's per-article guess wins (an Azerbaijani article searched as Turkish
            # finds little); the operator default is the fallback.
            "language": language or settings.SEARXNG_LANGUAGE,
        }
        if settings.SEARXNG_ENGINES.strip():
            params["engines"] = settings.SEARXNG_ENGINES.strip()

        try:
            async with httpx.AsyncClient(timeout=settings.SEARXNG_TIMEOUT) as client:
                resp = await client.get(f"{base}/search", params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — unreachable, timeout, bad JSON, 5xx…
            logger.warning("SearXNG search failed for %r at %s: %s", query, base,
                           type(exc).__name__)
            return []

        raw = payload.get("results") or []
        # SearXNG calls the snippet `content`. The malformed-URL drop (in results_from_hits)
        # mirrors the DuckDuckGo provider, which had to drop a relative Startpage tracking
        # redirect served by a degraded backend — a hit we cannot fetch is not a lead.
        results = results_from_hits(
            raw,
            lambda hit: (hit.get("url"), hit.get("title") or "", hit.get("content") or ""),
            query=query,
            provider="SearXNG",
            max_results=max_results,
        )

        logger.info("SearXNG returned %d usable hit(s) of %d for %r (engines=%s)",
                    len(results), len(raw), query, params.get("engines", "<default>"))
        return results
