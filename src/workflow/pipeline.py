# src/workflow/pipeline.py
"""The six-step audit orchestrator.

    1. classification     IPTC media topics (≤5, ranked) + genre + geographic scope — metadata,
                          never fails the audit
    2. similarity         find similar link: the archive's similarity index, fetched and measured —
                          a measured republication ends the audit as MOVED (steps 3–6 skipped)
    3. relevance          out of scope? — may short-circuit (LIKELY_LOW_PUBLIC_INTEREST) …
    4. related_removals   … unless other articles on the SAME subject vanished from the archive
                          within ±14 days: the RELATED_REMOVALS flag keeps the audit in scope
                          (operator's rule, 2026-09-30). Never fails the audit.
    5. source             the article itself (premise probe) + the outlet/agency it credits; a
                          dead cited source is the SOURCE_DEAD red flag
    6. web_search         is the story reachable elsewhere or on the publisher's site? Skipped
                          when step 5 found the article live at its own URL.

`_decide` is the ONLY place a verdict is chosen — from evidence, in plain code.
"""
import logging
import traceback
from typing import List, Optional

from src.api.models import (
    AuditFlag, AuditRequest, AuditResponse, Classification, StageName, StageOutcome, StageResult,
    Verdict,
)
from src.clients.concepts import ConceptCatalog
from src.search.base import BaseSearchProvider, SearchResult
from src.search.fetch import normalize_url
from src.search.textmatch import MOVED_THRESHOLD, PARTIAL_MIN
from src.workflow import classification as classification_stage
from src.workflow import related_removals as related_removals_stage
from src.workflow import relevance as relevance_stage
from src.workflow import similarity as similarity_stage
from src.workflow import source as source_stage
from src.workflow import web_search as web_search_stage
from src.workflow.agents import StageAgents
from src.workflow.web_search import WebSearchEvidence

logger = logging.getLogger(__name__)


def _log_stage_failure(what: str, url: str, exc: BaseException) -> None:
    """ERROR line with the exception TYPE only, plus the stack FRAMES at DEBUG.

    Never the exception message: model/SDK errors quote raw response bodies,
    upstream URLs and internal hostnames. The frames (file, line, code) locate the failure
    without carrying any of that.
    """
    logger.error("%s for %s: %s", what, url, type(exc).__name__)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("%s for %s: %s — stack:\n%s", what, url, type(exc).__name__,
                     "".join(traceback.format_tb(exc.__traceback__)))

ORDER = [StageName.CLASSIFICATION, StageName.SIMILARITY, StageName.RELEVANCE,
         StageName.RELATED_REMOVALS, StageName.SOURCE, StageName.WEB_SEARCH]


def _skipped(stage: StageName, reason: str) -> StageResult:
    """Build an inconclusive, skipped StageResult with a `Skipped: <reason>` explanation."""
    return StageResult(
        stage=stage,
        outcome=StageOutcome.INCONCLUSIVE,
        confidence=None,
        explanation=f"Skipped: {reason}",
        skipped=True,
    )


def _stage_failed(stage: StageName) -> StageResult:
    """A non-skipped stage that did not complete (the model failed)."""
    return StageResult(
        stage=stage,
        outcome=StageOutcome.INCONCLUSIVE,
        confidence=None,
        explanation="Stage did not complete: the analysis model failed.",
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


async def run_audit(
    request: AuditRequest,
    *,
    agents: StageAgents,
    search_provider: BaseSearchProvider,
    catalog: Optional[ConceptCatalog] = None,
    karakutu_client=None,
) -> AuditResponse:
    """The six-step audit: classification → similar link → out of scope? → related removals →
    source → web search.

    Step 1 is metadata and never fails the audit. Step 2 ends the audit as MOVED when the
    archive's matches contain a measured republication. Step 3 may short-circuit (out of
    scope) — unless step 4 finds a pattern of related removals, which keeps the audit going
    with the RELATED_REMOVALS flag. Step 6 is skipped when step 5 found the article live at
    its own URL (the premise does not hold). `_decide` alone turns the evidence into a verdict.
    """
    removed = request.removed_article
    flags: List[AuditFlag] = []

    # 1. classification — metadata; a crash here is logged, never propagated
    try:
        cls = await classification_stage.run(removed, agents.classification, catalog)
    except Exception as exc:  # noqa: BLE001
        _log_stage_failure("Classification stage crashed", removed.url, exc)
        cls = classification_stage.ClassificationOutcome(
            explanation=f"Stage did not complete ({type(exc).__name__}); no tags assigned.",
            details={"crashed": True})
    classification = cls.classification
    cls_stage = StageResult(
        stage=StageName.CLASSIFICATION,
        outcome=StageOutcome.PASS if classification is not None else StageOutcome.INCONCLUSIVE,
        confidence=None, explanation=cls.explanation,
        skipped=bool(cls.details.get("skipped")), details=cls.details)

    # 2. find similar link — archive candidates, fetched and measured; never fails the audit
    try:
        sim = await similarity_stage.run(removed, client=karakutu_client)
    except Exception as exc:  # noqa: BLE001
        _log_stage_failure("Similarity stage crashed", removed.url, exc)
        sim = similarity_stage.SimilarityEvidence(
            reason=f"the similar-link step crashed ({type(exc).__name__}); continuing "
                   "without archive candidates")
    sim_stage = StageResult(
        stage=StageName.SIMILARITY,
        outcome=StageOutcome.PASS if sim.relocated_to is not None else StageOutcome.INCONCLUSIVE,
        confidence=None, explanation=sim.reason, skipped=False, details=_similarity_details(sim))
    if sim.relocated_to is not None:
        # A similar link found directly in the archive's matches IS the relocation (measured:
        # old_in_new >= MOVED_THRESHOLD). Per the operator's rule (2026-09-09) — and the
        # auditors' own "Relocated" screening decision — the audit ends here as MOVED and
        # the remaining steps are skipped. `_decide` still words the verdict.
        o = sim.relocation_old_in_new or 0.0
        web = WebSearchEvidence(
            relocated_to=sim.relocated_to, relocation_old_in_new=sim.relocation_old_in_new,
            relocation_new_in_old=sim.relocation_new_in_old, matches=[sim.relocated_to],
            hits_searched=len(sim.hits), live_pages_read=len(sim.fetched.live),
            unreadable_count=len(sim.fetched.unreadable), reasoning=sim.reason)
        verdict, findings = _decide(web)
        reason = (f"step 2 found the relocation among the archive's matches ({o:.0%} of the "
                  "original's text contained) — the audit ends as MOVED")
        return AuditResponse(
            verdict=verdict,
            stages=[cls_stage, sim_stage, _skipped(StageName.RELEVANCE, reason),
                    _skipped(StageName.RELATED_REMOVALS, reason),
                    _skipped(StageName.SOURCE, reason), _skipped(StageName.WEB_SEARCH, reason)],
            short_circuited_at=StageName.SIMILARITY,
            findings=_with_classification(findings, classification),
            classification=classification, flags=[])

    # 3. out of scope? — the unchanged screen; a model failure is AUDIT_FAILED, never a 500
    try:
        rel = await relevance_stage.run(removed, agents.relevance)
    except Exception as exc:  # noqa: BLE001
        _log_stage_failure("Relevance stage failed", removed.url, exc)
        return _audit_failed(
            f"the relevance model failed ({type(exc).__name__})",
            stages=[cls_stage, sim_stage, _stage_failed(StageName.RELEVANCE),
                    _skipped(StageName.RELATED_REMOVALS, "relevance did not complete"),
                    _skipped(StageName.SOURCE, "relevance did not complete"),
                    _skipped(StageName.WEB_SEARCH, "relevance did not complete")],
            classification=classification)
    rel_stage = StageResult(
        stage=StageName.RELEVANCE,
        outcome=StageOutcome.PASS if rel.is_relevant else StageOutcome.FAIL,
        confidence=rel.confidence, explanation=rel.reasoning, skipped=False)

    # 4. related removals — did other articles on the SAME subject vanish within ±14 days?
    #    Runs whether or not step 3 passed: for an out-of-scope article the flag is what keeps
    #    the audit alive; for an in-scope one it is reviewer context. Never fails the audit.
    try:
        rr = await related_removals_stage.run(
            removed, agents.related_removals, client=karakutu_client,
            gist=classification.gist if classification else "")
    except Exception as exc:  # noqa: BLE001
        _log_stage_failure("Related-removals stage crashed", removed.url, exc)
        rr = related_removals_stage.RelatedRemovalsEvidence(
            reason=f"the related-removals step crashed ({type(exc).__name__})")
    if rr.flag:
        flags.append(AuditFlag.RELATED_REMOVALS)
        rr.kept_in_scope = not rel.is_relevant
    rr_stage = StageResult(
        stage=StageName.RELATED_REMOVALS,
        outcome=(StageOutcome.PASS if rr.flag
                 else StageOutcome.INCONCLUSIVE if not rr.available or rr.model_failed
                 else StageOutcome.FAIL),
        confidence=None, explanation=rr.reason, skipped=False, details=_related_details(rr))
    if not rel.is_relevant and not rr.flag:
        return AuditResponse(
            verdict=Verdict.LIKELY_LOW_PUBLIC_INTEREST,
            stages=[cls_stage, sim_stage, rel_stage, rr_stage,
                    _skipped(StageName.SOURCE, "relevance failed"),
                    _skipped(StageName.WEB_SEARCH, "relevance failed")],
            short_circuited_at=StageName.RELEVANCE,
            findings=_with_classification(
                f"Removed article judged not of public interest: {rel.reasoning} "
                + related_removals_stage.describe(rr), classification),
            classification=classification, flags=[])

    # 5. find the source — itself (premise probe) + a cited source; never fails the audit
    try:
        src = await source_stage.run(removed, agents.source, search_provider)
    except Exception as exc:  # noqa: BLE001
        _log_stage_failure("Source stage crashed", removed.url, exc)
        src = source_stage.SourceEvidence(
            reasoning=f"the source step crashed ({type(exc).__name__})")
    if src.dead_source is not None:
        flags.append(AuditFlag.SOURCE_DEAD)
    src_stage = StageResult(
        stage=StageName.SOURCE,
        outcome=(StageOutcome.PASS
                 if (src.live_at_original_url or src.source_status in ("live", "dead"))
                 else StageOutcome.INCONCLUSIVE),
        confidence=None, explanation=src.reasoning, skipped=False, details=_source_details(src))

    # 6. web search — skipped when the premise fails (a step-2 relocation never gets here)
    if src.live_at_original_url:
        web = WebSearchEvidence(
            live_at_original_url=True, original_url=removed.url,
            original_url_containment=src.self_containment,
            reasoning="Web search skipped: the removed URL answers with the article's text.")
        web_stage = _skipped(
            StageName.WEB_SEARCH,
            f"the removed URL is live with {(src.self_containment or 0):.0%} of the archived "
            "text present — the removal premise does not presently hold")
    else:
        prefetched = sim.fetched.model_copy(deep=True)
        leads: List[SearchResult] = list(sim.hits)
        if src.live_source is not None:
            prefetched.live.append(src.live_source)
            leads.append(SearchResult(url=src.live_source.url, title=src.live_source.title,
                                      snippet="", source="cited"))
        leads.extend(src.extra_hits)
        try:
            web = await web_search_stage.run(removed, search_provider, agents.web_search,
                                             prefetched=prefetched, extra_hits=leads)
        except web_search_stage.WebSearchModelFailure as exc:
            # The model failed every attempt. Do NOT 500, and do NOT pretend we found
            # nothing: this article was not audited. Surface that plainly so it gets re-run.
            # `exc`'s message is composed by web_search and carries only exception TYPE names.
            logger.error("Audit could not complete for %s: %s", removed.url, exc)
            return _audit_failed(
                f"the web-search model failed on every attempt ({exc})",
                stages=[cls_stage, sim_stage, rel_stage, rr_stage, src_stage,
                        _stage_failed(StageName.WEB_SEARCH)],
                classification=classification)
        web_stage = StageResult(
            stage=StageName.WEB_SEARCH,
            outcome=StageOutcome.PASS if web.matches else StageOutcome.FAIL,
            confidence=None, explanation=web.reasoning, skipped=False, details=_web_details(web))

    # The article's own cited source, when gone, is the strongest dead lead there is.
    if src.dead_source is not None and not any(
            normalize_url(d.url) == normalize_url(src.dead_source.url) for d in web.dead_same_story):
        web.dead_same_story.insert(0, src.dead_source)

    verdict, findings = _decide(web)
    if AuditFlag.SOURCE_DEAD in flags:
        who = f" ({src.source_name})" if src.source_name else ""
        findings += (
            f" RED FLAG (SOURCE_DEAD): the source this article itself credits{who}, "
            f"{src.dead_source.url}, answers "
            f"{src.dead_source.reason.replace('cited source — ', '')} — the origin copy of the "
            "story is gone too. This is a pointer for a human reviewer, not a finding of "
            "censorship."
        )
    if AuditFlag.RELATED_REMOVALS in flags:
        findings += " " + related_removals_stage.describe(rr)
        if rr.kept_in_scope:
            findings += (
                f" On its own the public-interest screen judged this article out of scope "
                f"({rel.reasoning}); the pattern of related removals kept it in the audit."
            )
    findings = _with_classification(findings, classification)
    return AuditResponse(
        verdict=verdict, stages=[cls_stage, sim_stage, rel_stage, rr_stage, src_stage, web_stage],
        short_circuited_at=None, findings=findings, classification=classification, flags=flags)


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
        "unreadable_count": web.unreadable_count,
        "matches": [m.url for m in web.matches],
        "dead_same_story": [{"url": d.url, "reason": d.reason, "source": d.source}
                            for d in web.dead_same_story],
        "relocated_to": web.relocated_to.url if web.relocated_to else None,
        "old_in_new": web.relocation_old_in_new, "new_in_old": web.relocation_new_in_old,
        "covers_key_facts": web.relocation_covers_key_facts, "omissions": web.omissions,
        "related_same_publisher": [r.url for r in web.related_same_publisher],
    }


def _decide(web) -> tuple[Verdict, str]:
    """Turn stage-3 evidence into a verdict. The ONLY place a verdict is chosen.

    The model never picks a verdict — it reports what pages say and which links are dead.
    This function alone decides what that means, which is what keeps a censorship claim out
    of the model's hands. `POSSIBLE_CENSORSHIP` is a REVIEW FLAG, never a finding.
    `NOT_REMOVED` is likewise descriptive: the probe found the article live at its own
    URL, which says nothing about what happened before.

    Relocation verdicts are tiered by MEASUREMENT where measurement was possible
    (src/search/textmatch.py — thresholds calibrated there): MOVED for an arithmetically
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
    return verdict, findings


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
    #    backend cannot support it yet, and that was measured, not assumed: with the rule on,
    #    the flag fired on 3 relocated-and-demonstrably-live articles (evrensel/98981 → 200,
    #    jinnews/270809 → 200, t24 /haber/) and on ZERO of the 3 human-confirmed censored ones.
    #    In every case ddgs simply never returned the relocated copy. One case flagged after
    #    reading a single medyascope infographic among 4 junk hits — that is a failed search,
    #    not a search that found nothing. With top-5 ddgs recall, "not found" measures the
    #    search's misses, not absence. Re-enable this on top of a real search API
    #    with measured recall, NOT before.
    if web.live_pages_read:
        return (
            Verdict.INCONCLUSIVE,
            f"Read {web.live_pages_read} live page(s) from {web.hits_searched} search hit(s), "
            "but none was confirmed to be the same story, and no copy was demonstrably gone. "
            "The search only covers the top results and its recall is known to be poor, so this "
            "is not evidence the story is absent. No inference of censorship.",
        )
    if web.unreadable_count:
        return (
            Verdict.INCONCLUSIVE,
            f"The search returned {web.hits_searched} link(s), but all {web.unreadable_count} "
            "were non-article files (PDFs, sitemaps), bot-blocked, or unreachable — no readable "
            "news page came back, so the story was never actually searched for. This is a "
            "SEARCH-QUALITY failure, not evidence about the removed article. "
            "No inference of censorship.",
        )
    return (
        Verdict.INCONCLUSIVE,
        f"The search returned no usable results at all ({web.hits_searched} hit(s)), so the "
        "story was never actually searched for. The search backend may have failed; that "
        "cannot be told apart from genuine absence. No inference of censorship.",
    )
