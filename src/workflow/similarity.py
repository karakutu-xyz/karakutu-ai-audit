"""Step 2 — find similar link: the archive's own similarity index, fetched and MEASURED.

The Karakutu archive scores similar articles server-side (`GET /articles/{id}/similar`).
Measured against 25 human-labelled relocations it returned the auditor's recorded new URL in
the top 5 for 24 (96%) — far better than web search (~33%). This step asks it, runs the
candidates through the same fetch gate as every other lead (self / variant / redirect-self
exclusion; live / dead / unreadable), and measures every live SAME-PUBLISHER candidate against
the removed article's prose (directional 5-token-shingle containment, src/search/textmatch.py).

No model is involved. A candidate with `old_in_new >= MOVED_THRESHOLD` is a measured
republication and is recorded as the relocation. A partial fit is recorded but NOT decided —
step 5's model and the calibrated tiers handle it exactly as before. Everything fetched here is
handed to step 5 (`fetched`, `hits`) so no URL is fetched twice and the archive-dead suppression
rule still applies there (archive candidates keep `source="archive"`).

Degrades: no wallet / unarchived URL / archive down => `available=False`, no candidates.
"""
import logging
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from src.api.models import Article
from src.clients.karakutu import KarakutuClient, get_client as get_karakutu_client
from src.search.base import SearchResult
from src.search.fetch import (
    FetchOutcome, LivePage, fetch_candidates, normalize_url, prose_of, same_publisher,
)
from src.search.textmatch import MOVED_THRESHOLD, SIBLING_MAX, directional_containments

logger = logging.getLogger(__name__)

ARCHIVE_CANDIDATES = 5


class SimilarCandidate(BaseModel):
    url: str
    title: str = ""
    score: float = 0.0                  # the archive's similarity_score
    same_publisher: bool = False
    status: str = "unfetched"           # live | dead | unreadable | excluded_self | excluded_variant | excluded_redirect_self
    final_url: Optional[str] = None
    old_in_new: Optional[float] = None  # measured for live same-publisher candidates only
    new_in_old: Optional[float] = None
    fit: Optional[str] = None           # verbatim | partial | sibling | unmeasured


class SimilarityEvidence(BaseModel):
    available: bool = False
    reason: str = ""
    candidates: List[SimilarCandidate] = Field(default_factory=list)
    relocated_to: Optional[SearchResult] = None
    relocation_old_in_new: Optional[float] = None
    relocation_new_in_old: Optional[float] = None
    hits: List[SearchResult] = Field(default_factory=list)      # archive leads for step 5
    fetched: FetchOutcome = Field(default_factory=FetchOutcome)  # handed to step 5, never re-fetched


async def run(removed: Article, *, client: Optional[KarakutuClient] = None) -> SimilarityEvidence:
    """Ask the archive for similar articles, fetch them, measure same-publisher live ones."""
    client = client if client is not None else get_karakutu_client()
    if not client.enabled:
        return SimilarityEvidence(reason="the archive client is disabled (no wallet keyfile), "
                                         "so no similar-link lookup was made")
    similar = await client.find_similar_by_url(removed.url, limit=ARCHIVE_CANDIDATES)
    if not similar:
        return SimilarityEvidence(reason="the archive returned no similar articles (URL not "
                                         "archived, nothing similar, or archive unreachable)")

    pairs = [
        (s, SearchResult(url=s.link, title=s.title or "", snippet=s.description or "",
                         source="archive"))
        for s in similar if s.link
    ]
    hits = [h for _, h in pairs]
    fetched = await fetch_candidates(hits, exclude_url=removed.url)

    live: Dict[str, LivePage] = {normalize_url(p.url): p for p in fetched.live}
    dead = {normalize_url(d.url) for d in fetched.dead}
    unreadable = {normalize_url(u.url) for u in fetched.unreadable}
    excluded = {}
    for status, urls in (("excluded_self", fetched.excluded_self),
                         ("excluded_variant", fetched.excluded_variant),
                         ("excluded_redirect_self", fetched.excluded_redirect_self)):
        for u in urls:
            excluded[normalize_url(u)] = status
    removed_prose = prose_of(removed.content)

    candidates: List[SimilarCandidate] = []
    best: Optional[Tuple[LivePage, float, float]] = None
    for s, hit in pairs:
        key = normalize_url(hit.url)
        cand = SimilarCandidate(url=hit.url, title=hit.title, score=s.similarity_score,
                                same_publisher=same_publisher(hit.url, removed.url))
        if key in live:
            page = live[key]
            cand.status, cand.final_url = "live", page.final_url
            if cand.same_publisher:
                o, n = directional_containments(removed_prose, page.text)
                cand.old_in_new, cand.new_in_old, cand.fit = o, n, _fit(o, n)
                if o is not None and o >= MOVED_THRESHOLD and (best is None or o > best[1]):
                    best = (page, o, n)
        elif key in dead:
            cand.status = "dead"
        elif key in unreadable:
            cand.status = "unreadable"
        elif key in excluded:
            cand.status = excluded[key]
        candidates.append(cand)

    evidence = SimilarityEvidence(available=True, candidates=candidates, hits=hits, fetched=fetched)
    if best is not None:
        page, o, n = best
        evidence.relocated_to = SearchResult(url=page.url, title=page.title,
                                             snippet=page.text[:200], source="archive")
        evidence.relocation_old_in_new, evidence.relocation_new_in_old = o, n
        evidence.reason = (f"measured republication at {page.url}: {o:.0%} of the removed "
                           f"article's text is contained in it")
        logger.info("similarity: measured relocation %s (old_in_new=%.3f, new_in_old=%.3f)",
                    page.url, o, n)
    else:
        n_live = sum(1 for c in candidates if c.status == "live")
        n_dead = sum(1 for c in candidates if c.status == "dead")
        evidence.reason = (f"{len(candidates)} archive candidate(s): {n_live} live, {n_dead} dead, "
                           f"{len(candidates) - n_live - n_dead} unreadable/excluded; no verbatim "
                           "same-publisher republication measured")
    return evidence


def _fit(o: Optional[float], n: Optional[float]) -> str:
    if o is None or n is None:
        return "unmeasured"
    if o >= MOVED_THRESHOLD:
        return "verbatim"
    if max(o, n) < SIBLING_MAX:
        return "sibling"
    return "partial"
