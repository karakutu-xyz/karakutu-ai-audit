"""Evidence-to-verdict rules and stage rendering used by the LangGraph engine.

    1. classification     IPTC media topics (≤5, ranked) + genre + geographic scope — metadata,
                          never fails the audit
    2. similarity         find similar link: the archive's similarity index, fetched and measured —
                          a measured republication ends the audit as MOVED (steps 3–6 skipped)
    3. relevance          out of scope? — may exit as LIKELY_LOW_PUBLIC_INTEREST or,
                          with a measured same-publisher partial copy holding ≥50% or a
                          qualifying rewrite, LIKELY_MOVED …
    4. related_removals   … unless other articles on the SAME subject vanished from the archive
                          within ±14 days: the RELATED_REMOVALS flag keeps the audit in scope
                          (operator's rule). Never fails the audit.
    5. source             the article itself (premise probe) + the outlet/agency it credits; a
                          dead cited source is the SOURCE_DEAD red flag
    6. web_search         is the story reachable elsewhere or on the publisher's site? Skipped
                          when step 5 found the article live at its own URL.

`_decide` chooses evidence verdicts in plain code, including measured low-interest relocations.
"""
import math
from typing import List, Optional

from audit.domain import (
    AuditResponse, Classification, StageName, StageOutcome, StageResult,
    Verdict,
)
from audit.search.rewrite import STUB_MAX_WORDS, RewriteSignal
from audit.search.textmatch import MOVED_THRESHOLD, PARTIAL_MIN
from audit.search.base import SearchResult
from audit.search.urls import same_publisher
from audit.workflow import related_removals, web_search
from audit.workflow.relevance import RelevanceResult
from audit.workflow.similarity import SimilarCandidate, SimilarityEvidence
from audit.workflow.web_search import candidate_counts, search_failure_note


ORDER = [StageName.CLASSIFICATION, StageName.SIMILARITY, StageName.RELEVANCE,
         StageName.RELATED_REMOVALS, StageName.SOURCE, StageName.WEB_SEARCH]

# Share of the removed text (old_in_new) a measured same-publisher copy must hold
# for LIKELY_MOVED at the low-interest exit; starting value, to be set from the benchmark.
LOW_INTEREST_RELOCATION_FLOOR = 0.50


def _best_partial_relocation(sim: SimilarityEvidence) -> Optional[SimilarCandidate]:
    """Pick the live, measured same-publisher partial holding most of the removed text.

    `same_publisher` is judged on the requested URL, but the text was measured on the page
    the request ended at, so a redirect off the publisher's host never counts.
    """
    return max(
        (c for c in sim.candidates
         if c.status == "live" and c.same_publisher and c.fit == "partial"
         and same_publisher(c.final_url or c.url, c.url)
         and c.old_in_new is not None and c.new_in_old is not None),
        key=lambda c: c.old_in_new,
        default=None,
    )


def _percent_down(share: float) -> str:
    """Whole percent rounded down, so a share below the floor never reads as the floor."""
    return f"{math.floor(share * 100)}%"


def _low_interest_response(
    sim: SimilarityEvidence, relevance: RelevanceResult,
    related: related_removals.RelatedRemovalsEvidence, *,
    stages: List[StageResult], classification: Optional[Classification],
) -> AuditResponse:
    """End an out-of-scope audit while retaining a partial relocation or rewrite."""
    candidate = _best_partial_relocation(sim)
    verdict = Verdict.LIKELY_LOW_PUBLIC_INTEREST
    findings = f"Removed article judged not of public interest: {relevance.reasoning} "
    if candidate is not None and candidate.old_in_new >= LOW_INTEREST_RELOCATION_FLOOR:
        deciding_candidate = candidate
        rewrite = None
    else:
        deciding_candidate = sim.rewrite
        rewrite = RewriteSignal(**sim.rewrite.model_dump()) if sim.rewrite else None
    if deciding_candidate is not None:
        relocated = SearchResult(
            url=deciding_candidate.url, title=deciding_candidate.title,
            snippet="", source="archive",
        )
        web = web_search.WebSearchEvidence(
            relocated_to=relocated,
            relocation_old_in_new=deciding_candidate.old_in_new,
            relocation_new_in_old=deciding_candidate.new_in_old,
            relocation_rewrite=rewrite,
            matches=[relocated], hits_searched=len(sim.hits),
            archive_candidates=len(sim.hits),
            live_pages_read=len(sim.fetched.live),
            unreadable_count=len(sim.fetched.unreadable), reasoning=sim.reason,
        )
        verdict, findings = _decide(web)
        kind = "same-publisher rewrite" if rewrite else "measured same-publisher copy"
        findings += (
            " The public-interest screen judged the article out of scope "
            f"({relevance.reasoning}); the {kind} from the "
            "similarity step decides the verdict instead. The key-facts comparison "
            "of web search was not run, so a person should compare the two pages. "
        )
    elif candidate is not None:
        o, n = candidate.old_in_new, candidate.new_in_old
        findings += (
            f"Relocation note: a same-publisher page at {candidate.url} holds "
            f"{_percent_down(o)} of the original's text (and {n:.0%} of that page comes from "
            f"the original), below the {LOW_INTEREST_RELOCATION_FLOOR:.0%} floor "
            "for a likely move; a person may want to compare the two. "
        )
    findings += related_removals.describe(related)
    return AuditResponse(
        verdict=verdict, stages=stages, short_circuited_at=StageName.RELEVANCE,
        findings=_with_classification(findings, classification),
        classification=classification, flags=[],
    )


def _skipped(stage: StageName, reason: str) -> StageResult:
    """Build an inconclusive, skipped StageResult with a `Skipped: <reason>` explanation."""
    return StageResult(
        stage=stage,
        outcome=StageOutcome.INCONCLUSIVE,
        confidence=None,
        explanation=f"Skipped: {reason}",
        skipped=True,
    )


MODEL_FAILED = "the analysis model failed"
STOPPED = "the audit was stopped"
UNEXPECTED_ERROR = "an unexpected error stopped this step"


def _stage_failed(stage: StageName, reason: str = MODEL_FAILED) -> StageResult:
    """A non-skipped stage that did not complete. The reason defaults to a model failure;
    a stop or an unexpected error must say so instead of blaming the model."""
    return StageResult(
        stage=stage,
        outcome=StageOutcome.INCONCLUSIVE,
        confidence=None,
        explanation=f"Stage did not complete: {reason}.",
        skipped=False,
    )


def _audit_failed(reason: str, *, stages: List[StageResult],
                  classification: Optional[Classification]) -> AuditResponse:
    """AUDIT_FAILED: the audit did NOT run. Not a result — the article must be re-run.

    Kept deliberately distinct from INCONCLUSIVE: a crashed audit filed as "we found nothing"
    is a silent false negative. Any model-failure path (relevance or web-search) ends here
    rather than as a 500 or a fabricated verdict.
    """
    return AuditResponse(
        verdict=Verdict.AUDIT_FAILED,
        stages=stages,
        short_circuited_at=None,
        findings=(
            f"AUDIT DID NOT COMPLETE — this is not a result. {reason}. The article has NOT "
            "been audited and must be re-run. Do not read this as evidence of anything, in "
            "either direction."
        ),
        classification=classification,
        flags=[],
    )


def _with_classification(findings: str, classification: Optional[Classification]) -> str:
    if classification is None or not classification.subject_tags:
        return findings
    parts = [", ".join(t.label for t in classification.subject_tags)]
    if classification.genre:
        parts.append(f"genre {classification.genre.label}")
    if classification.geographic_scope:
        parts.append(f"scope {classification.geographic_scope.value}")
    return findings + " Classification: " + "; ".join(parts) + "."


def _similarity_details(sim) -> dict:
    return {
        "available": sim.available, "reason": sim.reason,
        "candidates": [c.model_dump() for c in sim.candidates],
        "rewrite": sim.rewrite.model_dump() if sim.rewrite else None,
        "relocated_to": sim.relocated_to.url if sim.relocated_to else None,
        "old_in_new": sim.relocation_old_in_new, "new_in_old": sim.relocation_new_in_old,
    }


def _related_details(rr) -> dict:
    d = rr.model_dump()
    d["members"] = [m.model_dump() for m in rr.members]
    d["prefiltered"] = [m.model_dump() for m in rr.prefiltered]
    return d


def _source_details(src) -> dict:
    d = src.model_dump(exclude={"live_source", "dead_source", "extra_hits"})
    d["live_source"] = src.live_source.url if src.live_source else None
    d["dead_source"] = src.dead_source.model_dump() if src.dead_source else None
    d["extra_hits"] = [h.url for h in src.extra_hits]
    return d


def _web_details(web) -> dict:
    return {
        "hits_searched": web.hits_searched, "live_pages_read": web.live_pages_read,
        "search_queries": web.search_queries,
        "search_queries_failed": web.search_queries_failed,
        "web_hits": web.web_hits,
        "archive_candidates": web.archive_candidates,
        "cited_source_leads": web.cited_source_leads,
        "unreadable_count": web.unreadable_count,
        "matches": [m.url for m in web.matches],
        "dead_same_story": [{"url": d.url, "reason": d.reason, "source": d.source}
                            for d in web.dead_same_story],
        "relocated_to": web.relocated_to.url if web.relocated_to else None,
        "old_in_new": web.relocation_old_in_new, "new_in_old": web.relocation_new_in_old,
        "relocation_rewrite": (web.relocation_rewrite.model_dump()
                               if web.relocation_rewrite else None),
        "covers_key_facts": web.relocation_covers_key_facts, "omissions": web.omissions,
        "related_same_publisher": [r.url for r in web.related_same_publisher],
    }


def _decide(web) -> tuple[Verdict, str]:
    """Turn grounded web/source evidence into a substantive verdict.

    The model never picks a verdict — it reports what pages say and which links are dead.
    This function alone decides what that means, which is what keeps a censorship claim out
    of the model's hands. `POSSIBLE_CENSORSHIP` is a REVIEW FLAG, never a finding.
    `NOT_REMOVED` is likewise descriptive: the probe found the article live at its own
    URL, which says nothing about what happened before.

    Relocation verdicts are tiered by MEASUREMENT where measurement was possible
    (backend/audit/search/textmatch.py — thresholds calibrated there): MOVED for an arithmetically
    contained copy, LIKELY_MOVED for a partial or unmeasurable fit. Siblings the grounding
    demoted (`related_same_publisher`) are surfaced as reviewer context on every verdict.
    """
    verdict, findings = _decide_verdict(web)
    if web.related_same_publisher:
        urls = ", ".join(r.url for r in web.related_same_publisher)
        findings += (
            f" For reviewer context: {len(web.related_same_publisher)} same-publisher "
            f"page(s) on the same topic measured as a DIFFERENT article, not a "
            f"republication of the removed one, and were set aside: {urls}."
        )
    return verdict, search_failure_note(web) + findings


def _decide_verdict(web) -> tuple[Verdict, str]:
    # 0. The audit premise did not hold: the "removed" article answered live at its own
    #    URL with its text measurably intact. Established by the probe MEASUREMENT alone —
    #    no model involved. Descriptive and NOT exculpatory: removals get reversed, go
    #    intermittent or geo-dependent, and archive statuses go stale.
    if web.live_at_original_url:
        at = f" ({web.original_url})" if web.original_url else ""
        measured = (
            f" (measured: {web.original_url_containment:.0%} of the archived text "
            f"appears on the live page)"
            if web.original_url_containment is not None else ""
        )
        return (
            Verdict.NOT_REMOVED,
            f"The removed article is currently reachable at its original URL{at} with "
            f"its text substantially intact{measured}, so the audit premise — a removed "
            f"article — does not presently hold. It may have been RESTORED after the "
            f"archive recorded its removal, the removal may be intermittent or "
            f"geo-dependent, or the archive's status may be stale. A human should "
            f"re-check the URL and close or re-queue this audit. This is NOT a finding "
            f"that nothing was ever removed, and it is not exculpatory.",
        )

    # 1. A live copy at the same publisher under a new URL — the article moved. The tier
    #    inside this branch is decided by ARITHMETIC (directional shingle containment),
    #    never by the model — measurement is exactly what stops a same-cycle sibling, or
    #    our own excerpt truncation, from writing the verdict.
    if web.relocated_to is not None:
        o, n = web.relocation_old_in_new, web.relocation_new_in_old
        if o is not None and o >= MOVED_THRESHOLD:
            # MEASURED republication. A copy that arithmetically CONTAINS the original's
            # text cannot have been gutted, whatever the model claims — the
            # truncation-is-not-omission lesson, enforced by arithmetic.
            findings = (
                f"Measured republication: {o:.0%} of the original's text is contained in "
                f"the same-publisher copy at {web.relocated_to.url}; added or reworded "
                f"details in the copy do not change this."
            )
            if not web.relocation_covers_key_facts:
                findings += (
                    " A model-reported omission claim was set aside: the containment "
                    "measurement shows the original's text present in the copy."
                )
            return Verdict.MOVED, findings
        if web.relocation_rewrite is not None and web.relocation_covers_key_facts:
            signal = web.relocation_rewrite
            gap = signal.date_gap_days
            published = (
                "on the same day" if gap == 0
                else f"{gap} {'day' if gap == 1 else 'days'} apart"
            )
            findings = (
                f"Same-publisher rewrite at {web.relocated_to.url}: its headline shares "
                f"{_percent_down(signal.title_overlap)} of the original's headline words"
            )
            if signal.own_words < STUB_MAX_WORDS:
                findings += "; the original's text was too short to compare"
            else:
                findings += (
                    f", and {_percent_down(signal.word_overlap)} of the original's "
                    "distinctive words appear in it"
                )
            findings += f". The two pages were published {published}. "
            if signal.own_words >= STUB_MAX_WORDS and o is not None and n is not None:
                findings += (
                    f"The wording differs: {_percent_down(o)} of the original's text "
                    f"appears in the copy, and {_percent_down(n)} of the copy's text "
                    "comes from the original. "
                )
            return (Verdict.LIKELY_MOVED,
                    findings + "A human auditor should compare the two pages.")
        if o is None:
            # The fit could not be measured (no usable text on one side), so model
            # judgement stands — today's pre-measurement behaviour, said out loud.
            if web.relocation_covers_key_facts:
                return (
                    Verdict.LIKELY_MOVED,
                    f"Relocated by the same publisher to {web.relocated_to.url}, and the "
                    "republished copy still carries the original's key facts. The textual "
                    "fit could not be measured, so this rests on model judgement alone.",
                )
            missing = "; ".join(web.omissions) or "key facts of the original"
            return (
                Verdict.POSSIBLE_CENSORSHIP,
                f"FLAGGED FOR HUMAN REVIEW (not a finding of censorship). The article was "
                f"relocated by the same publisher to {web.relocated_to.url}, but the "
                f"republished copy materially omits: {missing}. The textual fit could not "
                f"be measured. A human auditor must compare the two and decide; this "
                f"service does not determine censorship.",
            )
        # Measured, but below the verbatim bar: a partial fit. The gutted-relocation flag
        # stays available in this band ONLY — with a measured-contained copy it is
        # arithmetically impossible, and a measured sibling never reaches here.
        pcts = (
            f"{o:.0%} of the original's text appears in the copy, and {n:.0%} of the "
            f"copy's text comes from the original"
        )
        if not web.relocation_covers_key_facts:
            missing = "; ".join(web.omissions) or "key facts of the original"
            return (
                Verdict.POSSIBLE_CENSORSHIP,
                f"FLAGGED FOR HUMAN REVIEW (not a finding of censorship). The article was "
                f"relocated by the same publisher to {web.relocated_to.url}, but the "
                f"republished copy materially omits: {missing}. Measured fit: {pcts}. A "
                f"human auditor must compare the two and decide; this service does not "
                f"determine censorship.",
            )
        fit = "substantial but partial" if max(o, n) >= PARTIAL_MIN else "weak"
        return (
            Verdict.LIKELY_MOVED,
            f"Relocated by the same publisher to {web.relocated_to.url} with a {fit} "
            f"textual fit: {pcts}. A human auditor should check whether the differences "
            f"matter.",
        )

    # 2. A live copy of the same story at another outlet. The story survives — which says
    #    nothing about why THIS publisher removed THEIR copy.
    if web.matches:
        urls = ", ".join(m.url for m in web.matches)
        note = (
            f"The same story is still online elsewhere: {urls}. Note this does NOT mean the "
            f"removal was benign — it means the story survives at another outlet."
        )
        if web.dead_same_story:
            # The story survives, but some copies of it have also gone. Surface that: a human
            # auditor wants to know a copy was taken down even when the story is not erased.
            dead = "; ".join(f"{d.url} ({d.reason})" for d in web.dead_same_story)
            note += (
                f" Also worth a human's attention: {len(web.dead_same_story)} other copy/copies "
                f"of this story are gone: {dead}."
            )
        return Verdict.FOUND_ONLINE, note

    # 3. Nothing alive, but a copy demonstrably existed and is now gone. This is the only
    #    evidence on which the service ever raises the flag.
    if web.dead_same_story:
        dead = "; ".join(
            f"{d.url} ({d.reason})" for d in web.dead_same_story
        )
        return (
            Verdict.POSSIBLE_CENSORSHIP,
            f"FLAGGED FOR HUMAN REVIEW (not a finding of censorship). No live copy of this "
            f"story could be found, and {len(web.dead_same_story)} copy/copies that appear to "
            f"be the same story are unreachable: {dead}. A dead link is not proof of anything "
            f"on its own — a human auditor must establish what happened.",
        )

    # 4. No same-story copy, alive or dead. The split that matters here is NOT "found vs not
    #    found" — it is "we looked and it isn't there" vs "we never managed to look".
    #
    #    TODO(not-found-flag): "we read pages and none matched" SHOULD raise the flag — an
    #    absent story is exactly what a human should triage. It is disabled because the search
    #    backend cannot support it yet: with the rule on, the flag fires on relocated articles
    #    that are demonstrably live at a new URL, simply because ddgs never returns the
    #    relocated copy, and it fires after reading a handful of unrelated junk hits — a failed
    #    search, not a search that found nothing. With shallow ddgs recall, "not found"
    #    measures the search's misses, not absence. Re-enable this on top of a real search API
    #    with measured recall, NOT before.
    if web.live_pages_read:
        return (
            Verdict.INCONCLUSIVE,
            f"Read {web.live_pages_read} live page(s) from {candidate_counts(web)}, "
            "but none was confirmed to be the same story, and no copy was demonstrably gone. "
            "The search only covers the top results and its recall is known to be poor, so this "
            "is not evidence the story is absent. No inference of censorship.",
        )
    if web.unreadable_count:
        return (
            Verdict.INCONCLUSIVE,
            f"The candidate list contained {candidate_counts(web)}, but all {web.unreadable_count} "
            "were non-article files (PDFs, sitemaps), bot-blocked, or unreachable — no readable "
            "news page came back, so the story was never actually searched for. This is a "
            "SEARCH-QUALITY failure, not evidence about the removed article. "
            "No inference of censorship.",
        )
    return (
        Verdict.INCONCLUSIVE,
        f"There were no usable results at all ({candidate_counts(web)}), so the "
        "story could not be checked against readable pages. No inference of censorship.",
    )
