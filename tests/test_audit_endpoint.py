"""POST /api/audit end-to-end through the ASGI app. Every stage `run` is patched at
`src.workflow.pipeline.<stage>_stage.run`; the lifespan's catalog loader is stubbed in conftest,
so no test opens a socket or calls a model."""
from unittest.mock import AsyncMock

import pytest

from src.api.models import Classification, ConceptTag
from src.search.base import SearchResult
from src.search.fetch import DeadCandidate
from src.workflow.classification import ClassificationOutcome
from src.workflow.related_removals import RelatedRemovalsEvidence
from src.workflow.relevance import RelevanceResult
from src.workflow.similarity import SimilarityEvidence
from src.workflow.source import SourceEvidence
from src.workflow.web_search import WebSearchEvidence, WebSearchModelFailure


@pytest.fixture
def five_stages(monkeypatch):
    """Canned, valid results for all six steps; tests override the one they care about."""
    fakes = {
        "classification": AsyncMock(return_value=ClassificationOutcome(
            classification=None, explanation="Skipped: no catalog", details={"skipped": True})),
        "similarity": AsyncMock(return_value=SimilarityEvidence(reason="no archive candidates")),
        "relevance": AsyncMock(return_value=RelevanceResult(is_relevant=True, confidence=0.8, reasoning="ok")),
        "related_removals": AsyncMock(return_value=RelatedRemovalsEvidence(available=True, model_confirmed=True, reason="no pattern")),
        "source": AsyncMock(return_value=SourceEvidence(self_status="dead_or_unreachable", reasoning="own")),
        "web_search": AsyncMock(return_value=WebSearchEvidence(
            matches=[SearchResult(url="https://a.example.com", title="A", snippet="...")], reasoning="found")),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(f"src.workflow.pipeline.{name}_stage.run", fake)
    return fakes


async def test_audit_happy_path_structure(client, valid_audit_request, five_stages):
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 200
    body = resp.json()
    assert [s["stage"] for s in body["stages"]] == ["classification", "similarity", "relevance", "related_removals", "source", "web_search"]
    assert body["verdict"] == "FOUND_ONLINE"
    assert "findings" in body
    assert "short_circuited_at" in body  # always present even when null
    assert body["classification"] is None
    assert body["flags"] == []
    assert body["stages"][0]["skipped"] is True   # no catalog in tests → classification skipped


async def test_short_circuit_relevance(client, valid_audit_request, five_stages):
    five_stages["relevance"].return_value = RelevanceResult(is_relevant=False, confidence=0.9, reasoning="not public interest")
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "LIKELY_LOW_PUBLIC_INTEREST"
    assert body["short_circuited_at"] == "relevance"
    assert [s["skipped"] for s in body["stages"]] == [True, False, False, False, True, True]
    five_stages["source"].assert_not_called()
    five_stages["web_search"].assert_not_called()


async def test_similar_articles_field_is_accepted_but_ignored(client, valid_audit_request, five_stages):
    # The contract still accepts up to 5 similar articles; the pipeline reads none of them —
    # step 2 asks the archive instead.
    for value in ([], None):
        valid_audit_request["similar_articles"] = value
        resp = await client.post("/api/audit", json=valid_audit_request)
        assert resp.status_code == 200
    valid_audit_request.pop("similar_articles")
    assert (await client.post("/api/audit", json=valid_audit_request)).status_code == 200


async def test_audit_rejects_six_similars(client, valid_audit_request):
    extra = dict(valid_audit_request["similar_articles"][0])
    valid_audit_request["similar_articles"].append(extra)
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 422


async def test_audit_rejects_missing_article_fields(client, valid_audit_request):
    valid_audit_request["removed_article"] = {"url": "https://x.example.com"}  # missing title, content
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 422


async def test_audit_flags_possible_censorship_when_a_same_story_copy_is_dead(client, valid_audit_request, five_stages):
    """End-to-end: relevance passes, nothing is live, but a same-story copy is dead."""
    five_stages["web_search"].return_value = WebSearchEvidence(
        dead_same_story=[DeadCandidate(url="https://t24.com.tr/haber/x", title="T", snippet="s",
                                       status=404, reason="HTTP 404")],
        hits_searched=5, reasoning="the only copy of this story is unreachable")
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "POSSIBLE_CENSORSHIP"
    assert "t24.com.tr/haber/x" in body["findings"]
    # A flag for a human, never a finding of censorship.
    assert "not a finding" in body["findings"].lower()
    assert "human auditor" in body["findings"].lower()


async def test_audit_reports_likely_moved_for_a_faithful_relocation(client, valid_audit_request, five_stages):
    moved = SearchResult(url="https://evrensel.net/yazi/98981/x", title="T", snippet="s")
    five_stages["web_search"].return_value = WebSearchEvidence(
        relocated_to=moved, relocation_covers_key_facts=True, matches=[moved], reasoning="republished intact")
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "LIKELY_MOVED"


async def test_a_model_failure_returns_audit_failed_not_a_500_and_not_inconclusive(client, valid_audit_request, five_stages):
    """A crashed audit must never 500, and must never be filed as a clean 'nothing found'."""
    five_stages["web_search"].side_effect = WebSearchModelFailure("the analysis model failed on all 3 attempts")
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 200, "a model failure must not surface as a 500"
    body = resp.json()
    assert body["verdict"] == "AUDIT_FAILED"
    assert "must be re-run" in body["findings"]
    assert "not a result" in body["findings"].lower()
    assert len(body["stages"]) == 6


async def test_a_relevance_model_failure_returns_audit_failed_not_a_500(client, valid_audit_request, five_stages):
    """Step 3 can fail (empty model response) just like step 5. It must return AUDIT_FAILED,
    never crash out as a 500, and never reach the later steps."""
    five_stages["relevance"].side_effect = RuntimeError("Received empty model response")
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 200, "a relevance model failure must not surface as a 500"
    body = resp.json()
    assert body["verdict"] == "AUDIT_FAILED"
    assert "relevance model failed" in body["findings"]
    assert "must be re-run" in body["findings"]
    five_stages["source"].assert_not_called()
    five_stages["web_search"].assert_not_called()


async def test_audit_reports_not_removed_when_the_article_is_live_at_its_url(client, valid_audit_request, five_stages):
    """Step 4's premise probe found the "removed" article alive at its own URL."""
    five_stages["source"].return_value = SourceEvidence(
        self_status="live_intact", self_containment=0.93, live_at_original_url=True,
        reasoning="the original URL still carries the article")
    resp = await client.post("/api/audit", json=valid_audit_request)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "NOT_REMOVED"
    assert "example.com/removed" in body["findings"]
    assert "not exculpatory" in body["findings"].lower()
    assert body["stages"][4]["outcome"] == "pass", "the probe conclusively established liveness"
    assert body["stages"][5]["skipped"] is True, "no search when the premise does not hold"
    assert body["short_circuited_at"] is None
    five_stages["web_search"].assert_not_called()


async def test_response_carries_classification_and_flags(client, valid_audit_request, five_stages):
    five_stages["classification"].return_value = ClassificationOutcome(
        classification=Classification(subject_tags=[ConceptTag(notation="medtop:20000093", label="corruption")], gist="g"),
        explanation="tagged", details={})
    dead = DeadCandidate(url="https://www.sozcu.com.tr/x", title="t", snippet="s", status=404,
                         reason="cited source — HTTP 404", source="cited")
    five_stages["source"].return_value = SourceEvidence(
        source_kind="other_outlet", source_name="Sözcü", source_url=dead.url, source_status="dead",
        dead_source=dead, reasoning="gone")
    resp = await client.post("/api/audit", json=valid_audit_request)
    body = resp.json()
    assert body["classification"]["subject_tags"][0]["notation"] == "medtop:20000093"
    assert body["flags"] == ["SOURCE_DEAD"]
    assert body["stages"][0]["details"] == {}
    assert body["stages"][4]["details"]["source_status"] == "dead"
    assert "RED FLAG (SOURCE_DEAD)" in body["findings"]


async def test_a_relocation_found_in_the_archive_ends_the_audit_as_moved(client, valid_audit_request, five_stages):
    moved = SearchResult(url="https://evrensel.net/yazi/98981/x", title="T", snippet="s", source="archive")
    five_stages["similarity"].return_value = SimilarityEvidence(
        available=True, reason="measured", relocated_to=moved, relocation_old_in_new=1.0, relocation_new_in_old=0.9, hits=[moved])
    resp = await client.post("/api/audit", json=valid_audit_request)
    body = resp.json()
    assert body["verdict"] == "MOVED" and body["short_circuited_at"] == "similarity"
    assert [s["skipped"] for s in body["stages"]] == [True, False, True, True, True, True]   # classification skipped: no catalog in tests
    five_stages["relevance"].assert_not_called()
    five_stages["web_search"].assert_not_called()
