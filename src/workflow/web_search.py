"""Step 5 — the web-evidence stage.

Search the web for the removed story, fetch the leads, and report what each fetched
page is. Earlier steps hand over what they already found and fetched (archive similarity
candidates from step 2, cited-source leads from step 4): those leads head the candidate
list and nothing already fetched is fetched again. This module NEVER decides a verdict —
it reports evidence and `pipeline._decide` alone turns that into a verdict. Reading order
below: models → agent builder → run orchestration → prompt/grounding helpers.
"""

import logging
from typing import List, Optional

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

from src.api.models import Article
from src.search.base import BaseSearchProvider, SearchResult
from src.search.fetch import (
    MAX_PAGE_CHARS,
    MAX_PAGES_IN_PROMPT,
    MAX_REMOVED_CHARS,
    DeadCandidate,
    FetchOutcome,
    LivePage,
    fetch_candidates,
    guess_language,
    host_of,
    normalize_url,
    prose_of,
    same_publisher,
)
from src.search.textmatch import SIBLING_MAX, directional_containments, distinctive_phrase
from src.workflow import prompts

logger = logging.getLogger(__name__)

# Load-bearing name (the `PLACEHOLDER_` prefix is a documented misnomer, kept for tests
# and AGENTS.md). The prompt text lives in `prompts.py`.
PLACEHOLDER_WEB_SEARCH_PROMPT = prompts.WEB_SEARCH_PROMPT

# Retry ladder for the model call: (pages_in_prompt, chars_per_page). A local model returns an
# EMPTY response when this prompt gets large, and it is the biggest prompt the service sends.
# START SMALL: a large first rung tends to come back empty on Turkish articles, burning a
# long doomed call before the ladder shrinks — the worst prompt would be tried FIRST. The first rung is now sized to usually SUCCEED (3 pages for coverage, but 500 chars),
# and the rungs shrink from there. Each rung is a genuinely smaller ask.
_PROMPT_BUDGETS = [
    (MAX_PAGES_IN_PROMPT, 500),   # 3 pages x 500
    (2, 400),
    (1, 300),
]
# Cap dead-link leads pasted into the prompt. Uncapped, they can push the prompt past the
# model's loaded context, and a human rarely needs more than a few leads.
MAX_DEAD_IN_PROMPT = 3
MAX_DEAD_SNIPPET_CHARS = 200
# Output cap per model call (merged over the provider default). A correct answer — up to 3
# matches with omissions plus two sentences of reasoning — is ~100–250 tokens. A runaway rung can
# otherwise run to the provider's token cap and take minutes before the next rung answers
# quickly. The ladder is the fix; the cap just makes the doomed rung cheap.
WEB_SEARCH_MAX_TOKENS = 700


# --- exceptions --------------------------------------------------------------

class WebSearchModelFailure(RuntimeError):
    """The analysis model failed on every attempt — the audit did not run.

    Distinct from "found nothing": the pipeline turns this into AUDIT_FAILED, never into
    INCONCLUSIVE. A crashed audit must be re-run, not filed as a clean result.
    """


# --- models ------------------------------------------------------------------

class PageMatch(BaseModel):
    """The model's assessment of one live page."""
    url: str
    covers_key_facts: bool = True
    omissions: List[str] = []


class WebSearchResult(BaseModel):
    """Raw, model-authored. Must be grounded against what was actually fetched."""
    matches: List[PageMatch] = []
    dead_same_story: List[str] = []
    reasoning: str


class WebSearchEvidence(BaseModel):
    """What stage 3 actually established. The pipeline turns this into a verdict.

    Deliberately NOT a verdict: this stage reports evidence, and `pipeline.run_audit` alone
    decides what it means. Keeping the judgement out of here is what stops the model from
    ever authoring a censorship claim.
    """
    # The audit-premise probe: is the "removed" article simply live at its OWN URL, with
    # its text measurably intact? Populated by the PIPELINE from step 4's probe (the source
    # stage fetches and measures the removed URL); THIS STAGE NEVER SETS THEM. When the
    # premise fails the pipeline skips this stage altogether, so every other field stays
    # empty. A URL the archive reports as removed can still answer 200 with the article intact.
    live_at_original_url: bool = False
    original_url: Optional[str] = None                 # the URL probed; set only with the flag
    original_url_containment: Optional[float] = None   # measured old_in_new vs the live page
    # A live copy at the SAME publisher under a different URL — i.e. a relocation.
    relocated_to: Optional[SearchResult] = None
    relocation_covers_key_facts: bool = True
    omissions: List[str] = []
    # Directional text containment MEASURED for the candidate that became `relocated_to`
    # (prose vs prose, src/search/textmatch.py): how much of the removed article's text
    # appears in the copy, and how much of the copy came from the removed article.
    # None = could not be measured; pipeline._decide then falls back to model judgement
    # and says so in the findings.
    relocation_old_in_new: Optional[float] = None
    relocation_new_in_old: Optional[float] = None
    # Same-publisher live pages the model CALLED a match but whose text MEASURES as a
    # different article (both containments < SIBLING_MAX): siblings from the same news
    # cycle, not relocations. Reviewer context only — never matches, never a relocation.
    related_same_publisher: List[SearchResult] = []
    # Live same-story copies anywhere (includes the relocation, if there is one).
    matches: List[SearchResult] = []
    # Dead links the model judged likely to have been the same story — the ONLY basis on
    # which this service ever flags an article for censorship review.
    dead_same_story: List[DeadCandidate] = []
    hits_searched: int = 0
    # How many pages were fetched and read (before same-story matching). Lets the pipeline
    # distinguish "read N pages, none was the same story" from "nothing was fetchable".
    live_pages_read: int = 0
    # How many hits were non-article files / blocked / unreachable — i.e. the search
    # returned links but none was a readable news page. Distinguishes "search returned junk"
    # from "the fetch failed". (When the search backend degrades it serves PDFs and sitemaps.)
    unreadable_count: int = 0
    reasoning: str = ""


# --- agent builder -----------------------------------------------------------

def build_agent(model: Model) -> Agent[None, WebSearchResult]:
    """Build the stage-3 evidence agent bound to the same-story prompt."""
    return Agent(model, result_type=WebSearchResult, system_prompt=PLACEHOLDER_WEB_SEARCH_PROMPT)


# --- run orchestration -------------------------------------------------------

async def run(removed: Article, search_provider: BaseSearchProvider,
              agent: Agent[None, WebSearchResult], *,
              prefetched: Optional[FetchOutcome] = None,
              extra_hits: Optional[List[SearchResult]] = None) -> WebSearchEvidence:
    """Search, fetch, and judge — returning grounded evidence about the removed story.

    `extra_hits` are leads earlier steps already found (archive similarity candidates,
    `source="archive"`; cited-source leads, `source="cited"`) — they lead the merged list.
    `prefetched` is what those steps already fetched; nothing in it is fetched again, and its
    archive-sourced dead candidates are upgraded to `source="web"` when a web pass here
    independently returns the same URL (their 404 then keeps full evidentiary standing).
    """
    hits = await _gather_hits(removed, search_provider, leads=list(extra_hits or []))

    # Search hits are only leads. Drop the removed article's own (still-indexed, dead) URL,
    # fetch the rest, and split them into live / dead / unreadable. See src/search/fetch.py.
    # Whatever earlier steps already fetched is reused as-is, not fetched twice.
    fetched = _merge_fetched(prefetched or FetchOutcome(),
                             await _fetch_new(hits, prefetched or FetchOutcome(), removed.url),
                             hits)

    web_dead = [d for d in fetched.dead if d.source != "archive"]
    if not fetched.live and not web_dead:
        # Nothing to read and nothing demonstrably gone. (Archive-sourced dead links do not
        # count: the archive catalogues removed articles, so those 404s prove nothing — see
        # _archive_dead_note.) The search may simply have failed — which is exactly why this
        # must not become a censorship flag.
        reasoning = (
            f"Nothing could be established: {len(hits)} search hit(s), of which "
            f"{len(fetched.unreadable)} were non-article files, blocked, or unreachable — "
            "so no readable news page was returned, and nothing was demonstrably removed. "
            "This is a search-quality problem, not evidence about the removed article."
        )
        archive_dead = len(fetched.dead) - len(web_dead)
        if archive_dead:
            reasoning += " " + _archive_dead_note(archive_dead)
        return WebSearchEvidence(
            hits_searched=len(hits),
            unreadable_count=len(fetched.unreadable),
            reasoning=reasoning,
        )

    # The model call is the one thing here that actually breaks: a local model handed the full
    # prompt (removed article + up to 3 fetched pages) returns an EMPTY response and pydantic-ai
    # raises. It is prompt-size driven, so retry progressively smaller before giving up.
    last_exc: Optional[Exception] = None
    for attempt, (pages, page_chars) in enumerate(_PROMPT_BUDGETS, start=1):
        try:
            prompt = _build_prompt(removed, fetched, max_pages=pages, page_chars=page_chars)
            result = await agent.run(prompt, model_settings={"max_tokens": WEB_SEARCH_MAX_TOKENS})
            if attempt > 1:
                logger.info("web_search: succeeded on attempt %d (%d page(s) x %d chars)",
                            attempt, pages, page_chars)
            return _ground(result.data, fetched, removed, len(hits))
        except Exception as exc:  # noqa: BLE001 — empty response, protocol error, timeout…
            last_exc = exc
            logger.warning("web_search: model attempt %d failed (%s); shrinking the prompt",
                           attempt, type(exc).__name__)

    # Every attempt failed. This is NOT "we found nothing" — the audit did not run. Raise so
    # the pipeline can mark it AUDIT_FAILED rather than file a crash as a clean verdict.
    raise WebSearchModelFailure(
        f"the analysis model failed on all {len(_PROMPT_BUDGETS)} attempts "
        f"(last: {type(last_exc).__name__})"   # type only: the message can quote the raw model reply
    ) from last_exc


# --- helpers -----------------------------------------------------------------

# How many hits to keep from each search pass, and overall (leads + three web passes).
_HITS_PER_PASS = 5
_MAX_HITS = 10


async def _gather_hits(removed: Article, search_provider: BaseSearchProvider,
                       leads: List[SearchResult]) -> List[SearchResult]:
    """Search passes, relocation-first, merged and de-duplicated.

    Candidate order: the LEADS earlier steps handed over first (the archive's similarity
    candidates from step 2 — it indexes these outlets continuously and usually ranks a
    relocation's new URL near the top — and cited-source leads from step 4), then the web passes below.

    The single most common case is a RELOCATION: the same publisher republishes the story at
    a new URL on its own domain (a section move /gundem/→/haber/, a new row id
    /yazi/12345→/yazi/12346). A plain title search competes against the whole web and can
    bury that copy entirely. So look on the publisher's own domain
    FIRST with a `site:` query, then do the general search for other-outlet copies and dead
    links. The domain-scoped hits lead so `same_publisher` gets its best shot at the
    relocation; the general pass fills in the rest. A third web pass searches ONE
    distinctive quoted phrase from the article's prose: a copy can be live on a major outlet
    while the bare-title search never surfaces it — near-identical sibling headlines drown the story, and an
    exact-phrase query cuts through the title collision.

    Every pass degrades to `[]` independently (a provider never raises), so a failed pass
    just contributes nothing.
    """
    passes: List[List[SearchResult]] = []

    # Pass 0 — leads from earlier steps (archive similarity candidates, cited sources).
    # source="archive" on a lead is load-bearing: the archive is a corpus OF REMOVED
    # articles, so its candidates are structurally biased toward 404s. They keep full
    # standing as LIVE matches/relocations, but a dead one must never become
    # dead_same_story evidence — that is exactly where false POSSIBLE_CENSORSHIP flags
    # come from. Cited leads keep source="cited" and full standing.
    if leads:
        passes.append(leads)

    # Language routing: guess from the removed URL (bizim.media / an /az/ segment -> "az").
    # An Azerbaijani article searched as Turkish finds little. None means "no guess" and providers fall back to their configured default.
    language = guess_language(removed.url)

    web_passes: List[List[SearchResult]] = []
    host = host_of(removed.url)
    if host:
        scoped = await search_provider.search(
            f"site:{host} {removed.title}", max_results=_HITS_PER_PASS, language=language,
        )
        web_passes.append(scoped)
        if scoped:
            logger.info("web_search: relocation pass (site:%s) returned %d hit(s)", host, len(scoped))

    general = await search_provider.search(removed.title, max_results=_HITS_PER_PASS,
                                           language=language)
    web_passes.append(general)

    # Pass 3 — ONE distinctive quoted phrase from the article's prose (see the docstring).
    # Degrades to nothing on short or empty content.
    phrase = distinctive_phrase(prose_of(removed.content))
    if phrase:
        quoted = await search_provider.search(f'"{phrase}"', max_results=_HITS_PER_PASS,
                                              language=language)
        web_passes.append(quoted)
        if quoted:
            logger.info('web_search: phrase pass ("%s") returned %d hit(s)', phrase, len(quoted))

    passes.extend(web_passes)

    # URLs a WEB pass surfaced independently. An archive candidate that a public search
    # engine also returns is upgraded to source="web": its discovery is no longer
    # archive-conditioned, so a 404 there keeps full evidentiary standing (the
    # a true positive where the republished text is shortened must survive even with the
    # archive enabled).
    web_urls = {normalize_url(h.url) for p in web_passes for h in p}

    # Merge relocation-pass-first, de-duplicating by normalized URL, capped at _MAX_HITS.
    # The first occurrence keeps its position; `source` survives the merge.
    merged: List[SearchResult] = []
    seen: set[str] = set()
    for hit in [h for p in passes for h in p]:
        key = normalize_url(hit.url)
        if key in seen:
            continue
        seen.add(key)
        if hit.source == "archive" and key in web_urls:
            hit = hit.model_copy(update={"source": "web"})
        merged.append(hit)
        if len(merged) >= _MAX_HITS:
            break
    return merged


async def _fetch_new(hits: List[SearchResult], prefetched: FetchOutcome, exclude_url: str) -> FetchOutcome:
    """Fetch only the hits earlier steps have not already fetched or excluded."""
    known = {normalize_url(u) for u in (
        [p.url for p in prefetched.live] + [d.url for d in prefetched.dead]
        + [u.url for u in prefetched.unreadable] + prefetched.excluded_self
        + prefetched.excluded_variant + prefetched.excluded_redirect_self)}
    new_hits = [h for h in hits if normalize_url(h.url) not in known]
    if not new_hits:
        return FetchOutcome()
    return await fetch_candidates(new_hits, exclude_url=exclude_url)


def _merge_fetched(prefetched: FetchOutcome, fresh: FetchOutcome, hits: List[SearchResult]) -> FetchOutcome:
    """Union of what earlier steps fetched and what this step fetched. An archive-sourced dead
    candidate a WEB pass independently returned is upgraded to source="web" (see _gather_hits)."""
    web_urls = {normalize_url(h.url) for h in hits if h.source == "web"}
    dead = [
        d.model_copy(update={"source": "web"}) if d.source == "archive" and normalize_url(d.url) in web_urls else d
        for d in prefetched.dead
    ] + fresh.dead
    return FetchOutcome(
        live=prefetched.live + fresh.live, dead=dead,
        unreadable=prefetched.unreadable + fresh.unreadable,
        excluded_self=prefetched.excluded_self + fresh.excluded_self,
        excluded_variant=prefetched.excluded_variant + fresh.excluded_variant,
        excluded_redirect_self=prefetched.excluded_redirect_self + fresh.excluded_redirect_self,
    )


def _build_prompt(removed: Article, fetched: FetchOutcome,
                  max_pages: int = MAX_PAGES_IN_PROMPT,
                  page_chars: int = MAX_PAGE_CHARS) -> str:
    """Render the removed article, live pages, and dead links into the model prompt."""
    live_text = "\n\n".join(
        f"- {p.title} ({p.url})\n  TEXT: {p.text[:page_chars]}"
        for p in fetched.live[:max_pages]
    ) or "(none)"

    # Only WEB-sourced dead links are shown: an archive-sourced dead link can never become
    # evidence (see _archive_dead_note), so showing it would only invite doomed citations
    # and crowd a genuine web 404 out of this capped list.
    web_dead = [d for d in fetched.dead if d.source != "archive"]
    dead_text = "\n".join(
        f"- {d.title} ({d.url}) — failed to load: {d.reason}\n"
        f"  SNIPPET: {d.snippet[:MAX_DEAD_SNIPPET_CHARS]}"
        for d in web_dead[:MAX_DEAD_IN_PROMPT]
    ) or "(none)"

    return (
        f"REMOVED ARTICLE:\nTitle: {removed.title}\n"
        f"Content: {removed.content[:MAX_REMOVED_CHARS]}\n\n"
        f"LIVE PAGES (fetched successfully; the removed article's own URL is already excluded):\n"
        f"{live_text}\n\n"
        f"DEAD LINKS (could not be loaded — you can only see title and snippet):\n{dead_text}"
    )


def _archive_dead_note(n: int) -> str:
    """Why archive-sourced dead links were set aside — said OUT LOUD in the evidence
    reasoning, so the pipeline's findings never silently claim "no copy was demonstrably
    gone" while a suppressed archive 404 sits in the fetch results."""
    return (
        f"({n} dead link(s) proposed by the archive's similarity index were set aside: the "
        "archive catalogues removed articles, so its candidates are expected to be dead, and "
        "a 404 there does not show that an independent copy of the story was taken down.)"
    )


def _ground(raw: WebSearchResult, fetched: FetchOutcome, removed: Article,
            hits_searched: int) -> WebSearchEvidence:
    """Resolve every model-authored URL back to something we actually fetched.

    `matches` is model-authored and its URLs end up in `AuditResponse.findings` as evidence.
    Told to copy URLs verbatim, a model can still mistype one — a spurious suffix such as
    `news.example.org.invalid/...` (a domain that does not resolve) for a page fetched at
    `news.example.org/...`. A fabricated
    citation in an evidence tool is worse than no citation, so anything that does not resolve
    to a fetched page is dropped.
    """
    live_by_url = {normalize_url(p.url): p for p in fetched.live}
    dead_by_url = {normalize_url(d.url): d for d in fetched.dead}

    matches: List[SearchResult] = []
    relocated_to: Optional[SearchResult] = None
    relocation_covers_key_facts = True
    omissions: List[str] = []
    relocation_old_in_new: Optional[float] = None
    relocation_new_in_old: Optional[float] = None
    related_same_publisher: List[SearchResult] = []
    invented: List[str] = []
    removed_prose = prose_of(removed.content)  # the archive hands content as raw HTML

    for m in raw.matches:
        page: Optional[LivePage] = live_by_url.get(normalize_url(m.url))
        if page is None:
            invented.append(m.url)
            continue
        hit = SearchResult(url=page.url, title=page.title, snippet=page.text[:200])

        # Relocation is decided HERE, in code, not by the model: same publisher, different
        # URL, and alive — a publisher's own relocation keeps the host. But "same publisher +
        # live + model says same story" is not enough: a model readily takes a same-cycle
        # SIBLING article for a copy.
        # So the candidate is MEASURED (directional shingle containment, prose vs prose)
        # before it may stand: one that is measurably a DIFFERENT article is demoted to
        # reviewer context, and the next same-publisher candidate gets its chance. An
        # UNMEASURABLE fit (no usable text on a side) never demotes — no evidence, no call.
        # Cross-publisher matches are never measured: different outlets legitimately rewrite.
        if relocated_to is None and same_publisher(page.url, removed.url):
            o, n = directional_containments(removed_prose, page.text)
            if o is not None and max(o, n) < SIBLING_MAX:
                logger.info(
                    "web_search: demoted same-publisher candidate %s — old_in_new=%.3f, "
                    "new_in_old=%.3f (both < %.2f): a sibling article, not a relocation",
                    page.url, o, n, SIBLING_MAX,
                )
                related_same_publisher.append(hit)
                continue   # not a same-story match, not a relocation
            relocated_to = hit
            relocation_old_in_new = o
            relocation_new_in_old = n
            relocation_covers_key_facts = m.covers_key_facts
            omissions = [om for om in m.omissions if om.strip()]
        matches.append(hit)

    # Archive-sourced dead candidates never enter dead_same_story — the only basis on which
    # the service ever raises the censorship flag. Web-sourced ones keep full standing.
    dead_same_story: List[DeadCandidate] = []
    suppressed_archive: List[str] = []
    for url in raw.dead_same_story:
        d = dead_by_url.get(normalize_url(url))
        if d is None:
            continue
        if d.source == "archive":
            suppressed_archive.append(d.url)
            continue
        dead_same_story.append(d)
    if suppressed_archive:
        logger.info(
            "web_search: suppressed %d archive-sourced dead candidate(s) from "
            "dead_same_story: %s", len(suppressed_archive), ", ".join(suppressed_archive),
        )

    reasoning = raw.reasoning
    archive_dead_count = sum(1 for d in fetched.dead if d.source == "archive")
    if archive_dead_count:
        reasoning = (reasoning + " " if reasoning else "") + _archive_dead_note(archive_dead_count)

    if invented:
        logger.warning(
            "web_search: dropped %d match(es) never fetched (model-invented URLs): %s",
            len(invented), ", ".join(invented),
        )

    return WebSearchEvidence(
        relocated_to=relocated_to,
        relocation_covers_key_facts=relocation_covers_key_facts,
        omissions=omissions,
        relocation_old_in_new=relocation_old_in_new,
        relocation_new_in_old=relocation_new_in_old,
        related_same_publisher=related_same_publisher,
        matches=matches,
        dead_same_story=dead_same_story,
        hits_searched=hits_searched,
        live_pages_read=len(fetched.live),
        unreadable_count=len(fetched.unreadable),
        reasoning=reasoning,
    )
