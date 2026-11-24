from abc import ABC, abstractmethod
from typing import List, Literal, Optional
from pydantic import BaseModel

SearchState = Literal["disabled", "unchecked", "ok", "unreachable", "rejected", "degraded"]


class SearchResult(BaseModel):
    url: str
    title: str
    snippet: str
    # Where the hit came from: "web" (a search pass) or "archive" (the Karakutu archive's
    # similarity index). Load-bearing for stage 3's dead-evidence gate: the archive is a
    # corpus OF REMOVED articles, so an archive-sourced candidate answering 404 is close to
    # a tautology and proves nothing about an independent copy of the story. Archive
    # candidates keep FULL standing as live matches/relocations (the index is the strongest
    # relocation lead); only web-sourced dead links may become dead_same_story evidence.
    source: str = "web"


class BaseSearchProvider(ABC):
    enabled = True

    async def search_checked(self, query: str, max_results: int = 5,
                             *, language: Optional[str] = None
                             ) -> tuple[List[SearchResult], bool]:
        """Return results and whether the query failed; providers never raise.

        Legacy providers that only implement search retain their successful/empty
        result contract. Providers that swallow failures must override this hook.
        """
        return await self.search(query, max_results, language=language), False

    @abstractmethod
    async def search(self, query: str, max_results: int = 5,
                     *, language: Optional[str] = None) -> List[SearchResult]:
        """Run one search. `language` is an optional ISO-639-1 hint guessed from the
        removed article's own URL (see `urls.guess_language`); providers that cannot use
        it accept and ignore it."""
