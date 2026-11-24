"""pipeline._decide — evidence in, verdict out.

The model never chooses a verdict; it reports what pages say and which links are dead.
This function alone decides what that means. These tests pin the tiering, and in
particular the two rules that keep the service inside its authority:

  * a dead link may raise a REVIEW FLAG, never a finding of censorship, and
  * silence (nothing live, nothing dead) must never raise anything at all.
"""

import pytest

from audit.domain import StageName, Verdict
from audit.search.base import SearchResult
from audit.search.fetch import DeadCandidate, FetchOutcome, LivePage, UnreadableCandidate
from audit.workflow import pipeline, related_removals
from audit.workflow.pipeline import (
    LOW_INTEREST_RELOCATION_FLOOR, _best_partial_relocation, _decide,
    _low_interest_response,
)
from audit.workflow.relevance import RelevanceResult
from audit.workflow.similarity import SimilarCandidate, SimilarityEvidence
from audit.workflow.web_search import WebSearchEvidence

RELOCATED = SearchResult(url="https://news.example.org/yazi/12346/x", title="t", snippet="s")
OTHER_OUTLET = SearchResult(url="https://www.post.example.org/haber/x", title="t", snippet="s")
SIBLING = SearchResult(url="https://news.example.org/haber/5900002/baska-bir-haber",
                       title="t", snippet="s")


def dead(url: str, reason: str = "HTTP 404") -> DeadCandidate:
    return DeadCandidate(url=url, title="t", snippet="s", status=404, reason=reason)


def test_relocated_and_faithful_is_likely_moved():
    evidence = WebSearchEvidence(
        relocated_to=RELOCATED, relocation_covers_key_facts=True,
        matches=[RELOCATED], reasoning="same story, intact",
    )

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.LIKELY_MOVED
    assert RELOCATED.url in findings


def test_relocated_but_gutted_is_flagged_for_review():
    # A quietly sanitized re-publish looks clean, so it must not be filed as a benign move.
    evidence = WebSearchEvidence(
        relocated_to=RELOCATED,
        relocation_covers_key_facts=False,
        omissions=["the bribery allegation against the mayor", "the 12 million TL figure"],
        matches=[RELOCATED],
        reasoning="republished without the allegation",
    )

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.POSSIBLE_CENSORSHIP
    assert "bribery allegation" in findings
    assert "12 million TL" in findings
    assert "not a finding" in findings.lower()   # a flag, never a conclusion
    assert "human" in findings.lower()


def test_a_live_copy_at_another_outlet_is_found_online():
    evidence = WebSearchEvidence(matches=[OTHER_OUTLET], reasoning="another outlet covers it")

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.FOUND_ONLINE
    assert OTHER_OUTLET.url in findings
    # FOUND_ONLINE must never read as exculpatory: the auditors' confirmed-censored samples
    # return exactly this, because the story survived at other outlets while the
    # publisher's own copy was removed.
    assert "does not mean the removal was benign" in findings.lower()


def test_no_live_copy_but_a_dead_same_story_copy_is_flagged_for_review():
    evidence = WebSearchEvidence(
        dead_same_story=[dead("https://wire.example.org/haber/x"), dead("https://daily.example.net/y")],
        hits_searched=5,
        reasoning="both copies unreachable",
    )

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.POSSIBLE_CENSORSHIP
    assert "wire.example.org/haber/x" in findings
    assert "not a finding" in findings.lower()
    assert "human auditor" in findings.lower()


def test_search_returned_only_junk_is_inconclusive_and_says_search_quality():
    # THE load-bearing test. A rate-limited search returns PDFs and sitemaps; if that became a
    # censorship flag, a broken backend would manufacture accusations. The findings must name
    # this as a SEARCH problem, not imply anything about the removed article.
    evidence = WebSearchEvidence(hits_searched=5, live_pages_read=0, unreadable_count=5,
                                 reasoning="all 5 hits were PDFs/blocked")

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.INCONCLUSIVE
    assert "search-quality" in findings.lower()
    assert "no inference of censorship" in findings.lower()


def test_search_returned_nothing_at_all_is_inconclusive():
    evidence = WebSearchEvidence(hits_searched=0, live_pages_read=0, unreadable_count=0,
                                 reasoning="empty")

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.INCONCLUSIVE
    assert "no usable results" in findings.lower()


def test_pages_read_but_none_matched_is_inconclusive_not_a_flag():
    """"Read pages, none matched" must NOT flag.

    With the flag ON, it fires on relocated articles that are demonstrably live (200 at
    their new URL) simply because the search never returns the
    relocated copy. With top-5 recall this rule measures the search's misses, not absence. See TODO(not-found-flag) in pipeline.py
    — re-enable only on a search backend with real recall.
    """
    evidence = WebSearchEvidence(hits_searched=5, live_pages_read=5,
                                 reasoning="read them; none was the same story")

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.INCONCLUSIVE
    assert "read 5 live page" in findings.lower()
    assert "not evidence the story is absent" in findings.lower()
    assert "no inference of censorship" in findings.lower()


def test_an_unreadable_page_alone_never_raises_the_flag():
    # A PDF or a JS shell is not a dead article. With no dead candidates, this is silence.
    evidence = WebSearchEvidence(hits_searched=4, dead_same_story=[], reasoning="all unreadable")

    verdict, _ = _decide(evidence)

    assert verdict is Verdict.INCONCLUSIVE


def test_relocation_takes_precedence_over_other_outlets():
    # If the publisher's own new copy exists, that is the story: it moved.
    evidence = WebSearchEvidence(
        relocated_to=RELOCATED, relocation_covers_key_facts=True,
        matches=[RELOCATED, OTHER_OUTLET], reasoning="both",
    )

    verdict, _ = _decide(evidence)

    assert verdict is Verdict.LIKELY_MOVED


def test_no_verdict_ever_asserts_censorship_as_fact():
    for evidence in (
        WebSearchEvidence(relocated_to=RELOCATED, relocation_covers_key_facts=False,
                          omissions=["x"], matches=[RELOCATED], reasoning="r"),
        WebSearchEvidence(dead_same_story=[dead("https://a.example/x")], reasoning="r"),
        WebSearchEvidence(matches=[OTHER_OUTLET], reasoning="r"),
        WebSearchEvidence(hits_searched=3, reasoning="r"),
        # New measured shapes must satisfy the same invariant.
        WebSearchEvidence(relocated_to=RELOCATED, relocation_old_in_new=1.0,
                          relocation_new_in_old=0.97, relocation_covers_key_facts=False,
                          matches=[RELOCATED], reasoning="r"),
        WebSearchEvidence(relocated_to=RELOCATED, relocation_old_in_new=0.85,
                          relocation_new_in_old=0.69, relocation_covers_key_facts=False,
                          omissions=["x"], matches=[RELOCATED], reasoning="r"),
        WebSearchEvidence(related_same_publisher=[SIBLING], live_pages_read=1,
                          hits_searched=5, reasoning="r"),
        WebSearchEvidence(live_at_original_url=True, original_url="https://a.example/x",
                          original_url_containment=0.9, reasoning="r"),
    ):
        _, findings = _decide(evidence)
        lowered = findings.lower()
        # The service may FLAG. It may never CONCLUDE.
        assert "was censored" not in lowered
        assert "is censored" not in lowered
        if "censorship" in lowered:
            assert ("not a finding" in lowered) or ("no inference" in lowered)


def test_found_online_still_surfaces_a_dead_copy_for_the_human():
    # The story survives, but a copy of it was taken down. A human auditor wants to know,
    # even though the verdict is not a flag.
    evidence = WebSearchEvidence(
        matches=[OTHER_OUTLET],
        dead_same_story=[dead("https://gazete.example/tr/x")],
        reasoning="alive at one outlet, gone at another",
    )

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.FOUND_ONLINE
    assert "gazete.example/tr/x" in findings
    assert "human" in findings.lower()


# --- measured relocation tiers (see the calibration shapes in textmatch.py) ---
#
# Prose-extracted on both sides: a verbatim republication contains the whole original
# (old_in_new == 1.0); a rewritten same-event story falls in the partial band; siblings sit
# near zero. Verdicts here are decided by arithmetic in _decide, never by the model.


def reloc_evidence(o, n, covers=True, omissions=(), extra_matches=()):
    return WebSearchEvidence(
        relocated_to=RELOCATED,
        relocation_covers_key_facts=covers,
        omissions=list(omissions),
        relocation_old_in_new=o,
        relocation_new_in_old=n,
        matches=[RELOCATED, *extra_matches],
        reasoning="same publisher, new URL",
    )


def test_a_measured_contained_copy_is_moved():
    verdict, findings = _decide(reloc_evidence(1.0, 0.97))

    assert verdict is Verdict.MOVED
    assert RELOCATED.url in findings
    assert "100%" in findings
    assert "measur" in findings.lower()


def test_measurement_overrides_a_model_gutted_claim():
    # The truncation-is-not-omission lesson, enforced by arithmetic: a copy that measurably
    # CONTAINS the original's text cannot have been gutted, whatever the model claims.
    verdict, findings = _decide(
        reloc_evidence(1.0, 0.92, covers=False, omissions=["the bribery allegation"]),
    )

    assert verdict is Verdict.MOVED
    assert "set aside" in findings.lower()
    assert "not a finding of censorship" not in findings  # not a flag at all


def test_a_partial_fit_is_likely_moved_with_both_percentages():
    verdict, findings = _decide(reloc_evidence(0.85, 0.69))

    assert verdict is Verdict.LIKELY_MOVED
    assert RELOCATED.url in findings
    assert "85%" in findings and "69%" in findings
    assert "human" in findings.lower()


def test_a_partial_fit_with_a_visible_gutting_claim_can_still_flag():
    # The gutted-relocation path survives, but ONLY in the measured partial band.
    verdict, findings = _decide(
        reloc_evidence(0.55, 0.80, covers=False, omissions=["the 12 million TL figure"]),
    )

    assert verdict is Verdict.POSSIBLE_CENSORSHIP
    assert "12 million TL" in findings
    assert "not a finding" in findings.lower()
    assert "55%" in findings and "80%" in findings


def test_an_unmeasured_relocation_keeps_likely_moved_and_says_so():
    verdict, findings = _decide(reloc_evidence(None, None))

    assert verdict is Verdict.LIKELY_MOVED
    assert RELOCATED.url in findings
    assert "could not be measured" in findings.lower()


def test_an_unmeasured_gutted_relocation_still_flags():
    # With no measurement available there is no arithmetic to override the model, so
    # today's behaviour stands.
    verdict, findings = _decide(
        reloc_evidence(None, None, covers=False, omissions=["the central allegation"]),
    )

    assert verdict is Verdict.POSSIBLE_CENSORSHIP
    assert "central allegation" in findings


def test_a_demoted_sibling_with_no_other_matches_falls_through_to_inconclusive():
    evidence = WebSearchEvidence(
        related_same_publisher=[SIBLING],
        matches=[],
        hits_searched=5,
        live_pages_read=1,
        reasoning="same publisher, same topic, different article",
    )

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.INCONCLUSIVE
    assert SIBLING.url in findings
    assert "different article" in findings.lower()
    assert "reviewer context" in findings.lower()


def test_a_demoted_sibling_with_another_outlet_match_is_still_found_online():
    evidence = WebSearchEvidence(
        matches=[OTHER_OUTLET],
        related_same_publisher=[SIBLING],
        hits_searched=5,
        live_pages_read=2,
        reasoning="another outlet carries it; the same-publisher hit is a sibling",
    )

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.FOUND_ONLINE
    assert OTHER_OUTLET.url in findings
    assert SIBLING.url in findings
    assert "different article" in findings.lower()


# --- NOT_REMOVED: the audit premise did not hold ------------------------------
#
# A "removed" URL can still answer 200 with the article
# intact. The stage-3 probe now detects this and the pipeline says so — descriptively,
# never as a finding about what did or did not happen before.


def test_live_at_original_url_is_not_removed():
    evidence = WebSearchEvidence(
        live_at_original_url=True,
        original_url="https://www.news.example.org/haber/5900003/belediye-meclisi-karari",
        original_url_containment=0.97,
        reasoning="the original URL still carries the article",
    )

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.NOT_REMOVED
    assert "news.example.org/haber/5900003" in findings
    assert "97%" in findings
    lowered = findings.lower()
    assert "does not presently hold" in lowered
    assert "restored" in lowered
    assert "stale" in lowered
    assert "re-check" in lowered
    assert "human" in lowered
    # The guardrail: reachable-today says nothing about what happened before.
    assert "not a finding that nothing was ever removed" in lowered
    assert "not exculpatory" in lowered


def test_not_removed_takes_precedence_over_every_other_tier():
    # Belt and braces: the stage skips search when the probe fires, so other evidence
    # should always be empty — but if any ever leaks in, the premise check still wins.
    evidence = WebSearchEvidence(
        live_at_original_url=True, original_url="https://x.example/a",
        original_url_containment=0.8,
        relocated_to=RELOCATED, matches=[RELOCATED, OTHER_OUTLET],
        dead_same_story=[dead("https://a.example/x")],
        reasoning="r",
    )

    verdict, _ = _decide(evidence)

    assert verdict is Verdict.NOT_REMOVED


def test_not_removed_without_a_measured_containment_still_reads_safely():
    evidence = WebSearchEvidence(live_at_original_url=True, reasoning="r")

    verdict, findings = _decide(evidence)

    assert verdict is Verdict.NOT_REMOVED
    assert "not exculpatory" in findings.lower()


def test_a_dead_cited_source_is_named_as_such_in_the_flag_findings():
    # A dead copy at the outlet the article ITSELF credits is filed like any other dead
    # same-story copy: the review flag, never a finding — and its reason names it.
    evidence = WebSearchEvidence(
        dead_same_story=[DeadCandidate(url="https://www.gazete.example/x", title="t", snippet="s",
                                       status=404, reason="cited source — HTTP 404", source="cited")],
        hits_searched=4, live_pages_read=1, reasoning="r")
    verdict, findings = _decide(evidence)
    assert verdict is Verdict.POSSIBLE_CENSORSHIP
    assert "cited source — HTTP 404" in findings and "not a finding of censorship" in findings


def partial_candidate(**changes):
    return SimilarCandidate(**{
        "url": "https://paper.example/copy", "title": "Invented copy",
        "status": "live", "same_publisher": True, "fit": "partial",
        "old_in_new": LOW_INTEREST_RELOCATION_FLOOR, "new_in_old": 0.41,
        **changes,
    })


def low_interest_response(sim):
    return _low_interest_response(
        sim,
        RelevanceResult(is_relevant=False, confidence=0.9, reasoning="Invented scope reason"),
        related_removals.RelatedRemovalsEvidence(reason="invented archive unavailable"),
        stages=[], classification=None,
    )


@pytest.mark.parametrize("changes", [
    {"same_publisher": False, "old_in_new": 0.98},
    {"fit": "unmeasured", "old_in_new": 0.98},
    {"fit": "sibling", "old_in_new": 0.01, "new_in_old": 0.01},
    {"fit": "verbatim", "old_in_new": 1.0},
    {"status": "dead", "old_in_new": 0.98},
    {"status": "unreadable", "old_in_new": 0.98},
    {"old_in_new": None},
    {"new_in_old": None},
])
def test_low_interest_ignores_candidates_without_live_measured_partial_fit(changes):
    sim = SimilarityEvidence(candidates=[partial_candidate(**changes)])
    assert _best_partial_relocation(sim) is None
    response = low_interest_response(sim)
    assert response.verdict is Verdict.LIKELY_LOW_PUBLIC_INTEREST
    assert response.findings == (
        "Removed article judged not of public interest: Invented scope reason "
        "Related removals: not checked (invented archive unavailable)."
    )


@pytest.mark.parametrize("old_in_new, verdict", [
    (LOW_INTEREST_RELOCATION_FLOOR, Verdict.LIKELY_MOVED),
    (LOW_INTEREST_RELOCATION_FLOOR - 0.001, Verdict.LIKELY_LOW_PUBLIC_INTEREST),
])
def test_low_interest_floor_and_best_partial(old_in_new, verdict):
    best = partial_candidate(old_in_new=old_in_new)
    sim = SimilarityEvidence(candidates=[
        partial_candidate(url="https://paper.example/less", old_in_new=0.34, new_in_old=0.99),
        partial_candidate(url="https://other.example/copy", same_publisher=False, old_in_new=0.98),
        partial_candidate(url="https://paper.example/unmeasured", fit="unmeasured", old_in_new=0.98),
        best,
    ])
    assert _best_partial_relocation(sim) == best
    response = low_interest_response(sim)
    assert response.verdict is verdict
    assert response.short_circuited_at is StageName.RELEVANCE
    assert response.flags == []
    assert best.url in response.findings
    assert "https://paper.example/less" not in response.findings
    assert "https://other.example/copy" not in response.findings
    assert "https://paper.example/unmeasured" not in response.findings
    assert "41%" in response.findings
    assert "Invented scope reason" in response.findings
    assert response.findings.endswith(
        "Related removals: not checked (invented archive unavailable)."
    )
    if verdict is Verdict.LIKELY_MOVED:
        assert "substantial but partial" in response.findings
        assert "out of scope" in response.findings
        assert "key-facts comparison of web search was not run" in response.findings
        assert "floor" not in response.findings
    else:
        assert "Relocation note:" in response.findings
        assert f"below the {LOW_INTEREST_RELOCATION_FLOOR:.0%} floor" in response.findings


def test_low_interest_reuses_existing_decision_with_archive_evidence(monkeypatch):
    candidate = partial_candidate()
    hit = SearchResult(url=candidate.url, title=candidate.title, snippet="", source="archive")
    sim = SimilarityEvidence(
        candidates=[candidate], hits=[hit], reason="Invented similarity reason",
        fetched=FetchOutcome(
            live=[LivePage(
                url=hit.url, final_url=hit.url, status=200,
                title=hit.title, text="Invented text",
            )],
            unreadable=[UnreadableCandidate(
                url="https://paper.example/blocked", title="Invented blocked page", reason="blocked",
            )],
        ),
    )
    decide = pipeline._decide
    seen = []

    def capture(web):
        seen.append(web)
        return decide(web)

    monkeypatch.setattr(pipeline, "_decide", capture)
    response = low_interest_response(sim)
    assert response.verdict is Verdict.LIKELY_MOVED
    assert len(seen) == 1
    web = seen[0]
    assert web.relocated_to == hit
    assert web.matches == [hit]
    assert web.relocation_old_in_new == candidate.old_in_new
    assert web.relocation_new_in_old == candidate.new_in_old
    assert web.relocation_covers_key_facts is True
    assert web.hits_searched == web.archive_candidates == 1
    assert web.live_pages_read == web.unreadable_count == 1
    assert web.reasoning == sim.reason
