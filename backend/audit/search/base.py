from abc import ABC, abstractmethod
from typing import List, Optional
from pydantic import BaseModel


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
    @abstractmethod
    async def search(self, query: str, max_results: int = 5,
                     *, language: Optional[str] = None) -> List[SearchResult]:
        """Run one search. `language` is an optional ISO-639-1 hint guessed from the
        removed article's own URL (see `urls.guess_language`); providers that cannot use
        it accept and ignore it."""
