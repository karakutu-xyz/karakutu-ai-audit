"""Step 2 — find similar link: the archive's own similarity index, fetched and MEASURED.

The Karakutu archive scores similar articles server-side (`GET /articles/{id}/similar`).
Because it indexes the publishers' own pages, it usually ranks a relocated article's new URL
among its top few matches — a far stronger lead than general web search. This step asks it, runs the
candidates through the same fetch gate as every other lead (self / variant / redirect-self
exclusion; live / dead / unreadable), and measures every live SAME-PUBLISHER candidate against
the removed article's prose (directional 5-token-shingle containment, `audit.search.textmatch`).

No model is involved. A candidate with `old_in_new >= MOVED_THRESHOLD` is a measured
republication and is recorded as the relocation. A measured partial same-publisher fit can
decide a low-interest exit as LIKELY_MOVED when it holds at least 50% of the removed text;
a qualifying rewrite can also decide that exit, and smaller partial fits otherwise add a
relocation note. Rewrites require headline and distinctive-word overlap within two days;
short originals require near-identical headlines within one day. Archive similarity scores
never establish this signal, and an unknown date gap cannot qualify. In-scope audits leave
partial fits and rewrites to web search's model and calibrated tiers. Everything fetched
here is handed to step 6 (`fetched`, `hits`) so no URL is fetched twice and the archive-dead suppression rule still applies there
(archive candidates keep `source="archive"`).

Degrades: no wallet / unarchived URL / archive down => `available=False`, no candidates.
"""
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from audit.domain import Article
from audit.clients.karakutu import KarakutuClient, get_client as get_karakutu_client
from audit.search.base import SearchResult
from audit.search.boilerplate import build_boilerplate, measure, own_text_words
from audit.search.fetch import (
    FetchOutcome, LivePage, fetch_candidates, normalize_url, prose_of, same_publisher,
)
from audit.search.rewrite import (
    STUB_MAX_WORDS, date_gap_days, is_rewrite, title_overlap, word_overlap,
)
from audit.search.textmatch import MOVED_THRESHOLD, SIBLING_MAX


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
    raw_old_in_new: Optional[float] = None
    raw_new_in_old: Optional[float] = None
    # Matched paragraph counts (proposed removals when capped): removed, candidate.
    boilerplate_paragraphs: Optional[Tuple[int, int]] = None
    boilerplate_capped: Optional[bool] = None
    title_overlap: Optional[float] = None
    title_overlap_reverse: Optional[float] = None
    word_overlap: Optional[float] = None
    own_words: Optional[int] = None
    date_gap_days: Optional[int] = None
    rewrite: bool = False
    fit: Optional[str] = None           # verbatim | partial | sibling | unmeasured


class SimilarityEvidence(BaseModel):
    available: bool = False
    reason: str = ""
    candidates: List[SimilarCandidate] = Field(default_factory=list)
    relocated_to: Optional[SearchResult] = None
    relocation_old_in_new: Optional[float] = None
    relocation_new_in_old: Optional[float] = None
    rewrite: Optional[SimilarCandidate] = None
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
    boilerplate = build_boilerplate(removed_prose, (
        p.text for p in live.values()
        if same_publisher(p.url, removed.url)
        and (not p.final_url or same_publisher(p.final_url, removed.url))
        and normalize_url(p.url) != normalize_url(removed.url)
    ))

    own_words = own_text_words(removed_prose, boilerplate)
    candidates: List[SimilarCandidate] = []
    best: Optional[Tuple[LivePage, float, float]] = None
    for s, hit in pairs:
        key = normalize_url(hit.url)
        cand = SimilarCandidate(url=hit.url, title=hit.title, score=s.similarity_score,
                                same_publisher=same_publisher(hit.url, removed.url))
        if key in live:
            page = live[key]
            cand.status, cand.final_url = "live", page.final_url
            cand.same_publisher = cand.same_publisher and (
                not page.final_url or same_publisher(page.final_url, removed.url)
            )
            if cand.same_publisher:
                measured = measure(removed_prose, page.text, boilerplate)
                o, n = measured.cleaned
                cand.old_in_new, cand.new_in_old, cand.fit = o, n, _fit(o, n)
                cand.raw_old_in_new, cand.raw_new_in_old = measured.raw
                cand.boilerplate_paragraphs = measured.boilerplate_paragraphs
                cand.boilerplate_capped = measured.boilerplate_capped
                cand.title_overlap, cand.title_overlap_reverse = title_overlap(
                    removed.title, removed.url, s.title or page.title or "", hit.url,
                )
                cand.word_overlap, cand.own_words = word_overlap(own_words, page.text)
                cand.date_gap_days = date_gap_days(
                    removed.published_at, s.publish_date,
                    s.date_proximity_days if "date_proximity_days" in s.model_fields_set else None,
                )
                cand.rewrite = cand.fit != "verbatim" and is_rewrite(
                    title_overlap=cand.title_overlap,
                    title_overlap_reverse=cand.title_overlap_reverse,
                    word_overlap=cand.word_overlap, own_words=cand.own_words,
                    date_gap_days=cand.date_gap_days,
                )
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
    else:
        evidence.rewrite = max(
            (c for c in candidates if c.rewrite),
            key=lambda c: (c.word_overlap if c.own_words >= STUB_MAX_WORDS
                           else c.title_overlap, c.title_overlap),
            default=None,
        )
        n_live = sum(1 for c in candidates if c.status == "live")
        n_dead = sum(1 for c in candidates if c.status == "dead")
        evidence.reason = (f"{len(candidates)} archive candidate(s): {n_live} live, {n_dead} dead, "
                           f"{len(candidates) - n_live - n_dead} unreadable/excluded; no verbatim "
                           "same-publisher republication measured")
        if evidence.rewrite:
            evidence.reason += (
                f". A same-publisher rewrite candidate was found at {evidence.rewrite.url}"
            )
    return evidence


def _fit(o: Optional[float], n: Optional[float]) -> str:
    if o is None or n is None:
        return "unmeasured"
    if o >= MOVED_THRESHOLD:
        return "verbatim"
    if max(o, n) < SIBLING_MAX:
        return "sibling"
    return "partial"
