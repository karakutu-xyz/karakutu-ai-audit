"""run_audit — the six steps in order, the skip rules, and how the pieces feed _decide.
Every stage's `run` is patched at `src.workflow.pipeline.<stage>.run`; no model, no socket."""
from unittest.mock import AsyncMock

import pytest

from src.api.models import (Article, AuditFlag, AuditRequest, Classification, ConceptTag,
                            GeographicScope, StageName, Verdict)
from src.search.base import SearchResult
from src.search.fetch import DeadCandidate, FetchOutcome, LivePage
from src.workflow import pipeline
from src.workflow.agents import StageAgents
from src.workflow.classification import ClassificationAgents, ClassificationOutcome
from src.workflow.related_removals import RelatedRemoval, RelatedRemovalsEvidence
from src.workflow.relevance import RelevanceResult
from src.workflow.similarity import SimilarityEvidence
from src.workflow.source import SourceEvidence
from src.workflow.web_search import WebSearchEvidence

REMOVED = Article(url="https://evrensel.net/yazi/98980/eski", title="Başlık", content="<p>metin</p>")
RELOC = SearchResult(url="https://evrensel.net/yazi/98981/yeni", title="t", snippet="s", source="archive")
CLS = Classification(subject_tags=[ConceptTag(notation="medtop:20000093", label="corruption")],
                     genre=ConceptTag(notation="genre:Current", label="Current Events"),
                     geographic_scope=GeographicScope.NATIONAL, gist="A probe.")
AGENTS = StageAgents(classification=ClassificationAgents(coarse=object(), fine=object()),
                     relevance=object(), related_removals=object(), source=object(), web_search=object())
PATTERN = RelatedRemovalsEvidence(
    available=True, flag=True, model_confirmed=True, reason="pattern",
    members=[RelatedRemoval(audit_id=1, title="Borsadaki fon çöküşü", newspaper="Evrensel", created_at=1789000000),
             RelatedRemoval(audit_id=2, title="fon krizindeki holdingler", newspaper="T24", decision="censored", created_at=1789100000),
             RelatedRemoval(audit_id=3, title="Tera Holding yöneticileri", newspaper="Evrensel", created_at=1789200000)],
    publishers={"Evrensel": 2, "T24": 1}, human_censored=[2])


@pytest.fixture
def stages(monkeypatch):
    fakes = {
        "classification": AsyncMock(return_value=ClassificationOutcome(
            classification=CLS, explanation="tagged", details={"gist": "A probe."})),
        "similarity": AsyncMock(return_value=SimilarityEvidence(available=True, reason="nothing verbatim")),
        "relevance": AsyncMock(return_value=RelevanceResult(is_relevant=True, confidence=0.9, reasoning="in scope")),
        "related_removals": AsyncMock(return_value=RelatedRemovalsEvidence(available=True, model_confirmed=True, reason="no pattern")),
        "source": AsyncMock(return_value=SourceEvidence(self_status="dead_or_unreachable", source_kind="self",
                                                        reasoning="own reporting")),
        "web_search": AsyncMock(return_value=WebSearchEvidence(hits_searched=3, live_pages_read=1,
                                                               reasoning="none matched")),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(f"src.workflow.pipeline.{name}_stage.run", fake)
    return fakes


async def run():
    return await pipeline.run_audit(AuditRequest(removed_article=REMOVED), agents=AGENTS,
                                    search_provider=object(), catalog=None)


async def test_six_stages_in_order_and_the_classification_surfaces(stages):
    r = await run()
    assert [s.stage for s in r.stages] == [StageName.CLASSIFICATION, StageName.SIMILARITY, StageName.RELEVANCE,
                                          StageName.RELATED_REMOVALS, StageName.SOURCE, StageName.WEB_SEARCH]
    assert r.classification == CLS
    assert r.stages[0].details == {"gist": "A probe."}
    assert r.flags == [] and r.verdict is Verdict.INCONCLUSIVE
    assert "Classification: corruption" in r.findings


async def test_out_of_scope_short_circuits_after_classification_and_similarity(stages):
    stages["relevance"].return_value = RelevanceResult(is_relevant=False, confidence=0.9, reasoning="routine")
    r = await run()
    assert r.verdict is Verdict.LIKELY_LOW_PUBLIC_INTEREST and r.short_circuited_at is StageName.RELEVANCE
    assert [s.skipped for s in r.stages] == [False, False, False, False, True, True]   # step 4 still ran
    assert r.classification == CLS           # steps 1–2 ran and are reported
    stages["related_removals"].assert_called_once()
    assert "Related removals: no pattern" in r.findings
    stages["source"].assert_not_called()
    stages["web_search"].assert_not_called()


async def test_a_related_removals_pattern_keeps_an_out_of_scope_article_in_the_audit(stages):
    # The operator's rule (2026-09-30): other articles on the same subject vanishing in the same
    # fortnight is a reason to keep auditing even when the topic alone would not qualify.
    stages["relevance"].return_value = RelevanceResult(is_relevant=False, confidence=0.9, reasoning="routine sports note")
    stages["related_removals"].return_value = PATTERN.model_copy(deep=True)
    r = await run()
    assert r.short_circuited_at is None and r.verdict is Verdict.INCONCLUSIVE
    assert r.flags == [AuditFlag.RELATED_REMOVALS]
    assert [s.skipped for s in r.stages] == [False] * 6
    assert r.stages[2].outcome.value == "fail" and r.stages[3].outcome.value == "pass"
    assert r.stages[3].details["kept_in_scope"] is True
    stages["source"].assert_called_once()
    stages["web_search"].assert_called_once()
    assert "RELATED REMOVALS (pattern flag)" in r.findings and "[human decision: censored]" in r.findings
    assert "kept it in the audit" in r.findings and "routine sports note" in r.findings
    assert "not a finding" in r.findings


async def test_a_pattern_on_an_in_scope_article_is_context_not_a_rescue(stages):
    stages["related_removals"].return_value = PATTERN.model_copy(deep=True)
    r = await run()
    assert r.flags == [AuditFlag.RELATED_REMOVALS] and r.stages[3].details["kept_in_scope"] is False
    assert "kept it in the audit" not in r.findings and "RELATED REMOVALS (pattern flag)" in r.findings


async def test_a_crashed_related_removals_step_never_fails_the_audit(stages):
    stages["related_removals"].side_effect = RuntimeError("archive hiccup")
    r = await run()
    assert r.verdict is Verdict.INCONCLUSIVE and r.flags == []
    assert r.stages[3].outcome.value == "inconclusive" and "crashed" in r.stages[3].explanation


async def test_a_crashed_related_removals_step_cannot_rescue_an_out_of_scope_article(stages):
    stages["relevance"].return_value = RelevanceResult(is_relevant=False, confidence=0.9, reasoning="routine")
    stages["related_removals"].side_effect = RuntimeError("archive hiccup")
    r = await run()
    assert r.verdict is Verdict.LIKELY_LOW_PUBLIC_INTEREST and r.short_circuited_at is StageName.RELEVANCE


async def test_a_measured_relocation_in_step_two_ends_the_audit_as_moved(stages):
    # The maintainers' rule (2026-09-09): a similar link found directly from the archive's matches IS
    # the relocation — set MOVED and skip the remaining steps, like the auditors' own
    # "Relocated" screening decision.
    stages["similarity"].return_value = SimilarityEvidence(
        available=True, reason="measured", relocated_to=RELOC, relocation_old_in_new=1.0,
        relocation_new_in_old=0.8, hits=[RELOC],
        fetched=FetchOutcome(live=[LivePage(url=RELOC.url, final_url=RELOC.url, status=200, title="t", text="x")]))
    r = await run()
    assert r.verdict is Verdict.MOVED
    assert r.short_circuited_at is StageName.SIMILARITY
    assert [s.skipped for s in r.stages] == [False, False, True, True, True, True]
    assert all("relocation" in s.explanation for s in r.stages[2:])
    stages["relevance"].assert_not_called()
    stages["related_removals"].assert_not_called()
    stages["source"].assert_not_called()
    stages["web_search"].assert_not_called()
    assert RELOC.url in r.findings and "100%" in r.findings
    assert r.classification == CLS and "Classification: corruption" in r.findings


async def test_live_at_its_own_url_skips_web_search_and_is_not_removed(stages):
    stages["source"].return_value = SourceEvidence(self_status="live_intact", self_containment=0.97,
                                                   live_at_original_url=True, source_kind="self", reasoning="live")
    r = await run()
    assert r.verdict is Verdict.NOT_REMOVED and r.stages[5].skipped is True
    assert "not exculpatory" in r.findings
    stages["web_search"].assert_not_called()


async def test_a_partial_archive_fit_does_not_short_circuit(stages):
    # Only a MEASURED relocation (old_in_new >= MOVED_THRESHOLD) ends the audit at step 2; a
    # partial fit is recorded and left to step 5's model + tiers as before.
    stages["similarity"].return_value = SimilarityEvidence(available=True, reason="partial only", hits=[RELOC])
    r = await run()
    assert r.short_circuited_at is None
    stages["relevance"].assert_called_once()
    stages["web_search"].assert_called_once()


async def test_a_dead_cited_source_flags_and_becomes_dead_evidence(stages):
    dead = DeadCandidate(url="https://www.sozcu.com.tr/x", title="t", snippet="s", status=404,
                         reason="cited source — HTTP 404", source="cited")
    stages["source"].return_value = SourceEvidence(source_kind="other_outlet", source_name="Sözcü",
                                                   source_url=dead.url, source_status="dead",
                                                   dead_source=dead, reasoning="gone")
    r = await run()
    assert r.flags == [AuditFlag.SOURCE_DEAD]
    assert r.verdict is Verdict.POSSIBLE_CENSORSHIP
    assert "RED FLAG (SOURCE_DEAD)" in r.findings and dead.url in r.findings
    assert "not a finding of censorship" in r.findings


async def test_a_dead_cited_source_with_a_live_copy_elsewhere_stays_found_online_but_flagged(stages):
    dead = DeadCandidate(url="https://www.sozcu.com.tr/x", title="t", snippet="s", status=404,
                         reason="cited source — HTTP 404", source="cited")
    stages["source"].return_value = SourceEvidence(source_kind="other_outlet", source_name="Sözcü",
                                                   source_url=dead.url, source_status="dead", dead_source=dead)
    stages["web_search"].return_value = WebSearchEvidence(
        matches=[SearchResult(url="https://birgun.net/haber/y", title="t", snippet="s")], reasoning="found")
    r = await run()
    assert r.verdict is Verdict.FOUND_ONLINE and r.flags == [AuditFlag.SOURCE_DEAD]
    assert "sozcu.com.tr/x" in r.findings


async def test_step_five_receives_prefetched_pages_and_leads(stages):
    page = LivePage(url="https://www.sozcu.com.tr/x", final_url="https://www.sozcu.com.tr/x",
                    status=200, title="t", text="x")
    stages["similarity"].return_value = SimilarityEvidence(available=True, hits=[RELOC], fetched=FetchOutcome(dead=[]))
    stages["source"].return_value = SourceEvidence(
        source_kind="other_outlet", source_name="Sözcü", source_url=page.url, source_status="live",
        live_source=page,
        extra_hits=[SearchResult(url="https://aa.com.tr/z", title="t", snippet="s", source="cited")])
    await run()
    kwargs = stages["web_search"].call_args.kwargs
    assert [p.url for p in kwargs["prefetched"].live] == [page.url]
    assert [h.url for h in kwargs["extra_hits"]] == [RELOC.url, page.url, "https://aa.com.tr/z"]


async def test_a_crashed_classification_or_similarity_never_fails_the_audit(stages):
    stages["classification"].side_effect = RuntimeError("boom")
    stages["similarity"].side_effect = RuntimeError("boom")
    r = await run()
    assert r.verdict is Verdict.INCONCLUSIVE and r.classification is None
    assert r.stages[0].outcome.value == "inconclusive" and r.stages[1].outcome.value == "inconclusive"


async def test_relevance_model_failure_is_audit_failed_with_six_stages(stages):
    stages["relevance"].side_effect = RuntimeError("empty")
    r = await run()
    assert r.verdict is Verdict.AUDIT_FAILED and len(r.stages) == 6 and r.classification == CLS
    stages["related_removals"].assert_not_called()


# --- raw exception text never reaches the findings or the ERROR log ---------

SECRET = "http://10.0.0.7:1234/v1 said: {'api_key': 'sk-live-XYZ', 'body': 'raw model reply'}"


async def test_a_relevance_failure_names_the_exception_type_but_never_its_text(stages, caplog):
    stages["relevance"].side_effect = RuntimeError(SECRET)
    with caplog.at_level("DEBUG", logger="src.workflow.pipeline"):
        r = await run()
    assert r.verdict is Verdict.AUDIT_FAILED
    assert "the relevance model failed (RuntimeError)" in r.findings
    assert "sk-live-XYZ" not in r.findings and "10.0.0.7" not in r.findings
    assert "sk-live-XYZ" not in caplog.text, "the exception message leaked into the logs"
    assert any(rec.levelname == "ERROR" and "RuntimeError" in rec.getMessage()
               for rec in caplog.records)


@pytest.mark.parametrize("stage", ["classification", "similarity", "related_removals", "source"])
async def test_a_crashed_stage_logs_its_type_and_never_its_message(stages, caplog, stage):
    stages[stage].side_effect = ValueError(SECRET)
    with caplog.at_level("DEBUG", logger="src.workflow.pipeline"):
        await run()
    errors = [rec.getMessage() for rec in caplog.records if rec.levelname == "ERROR"]
    assert errors and all("ValueError" in m for m in errors)
    assert "sk-live-XYZ" not in caplog.text
