"""Deterministic, entirely fictional review records for an isolated test-mode database.

No network or model is used. Similarity is measured without a model, as in production.
Fixed demo IDs are restored on every seed; non-demo records are never touched. Only a
test database is accepted (see `is_test_database`).
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from audit.clients.concepts import Concept
from audit.config import has_demo_marker, is_test_database_path
from audit.db import (
    ACTIVATION_ORDER, ArticleRow, article_values, ERRORS, PromptActivation, PromptVersion, Reaction, Run, Store, Workflow,
)
from audit.domain import (
    Article, AuditFlag, AuditRequest, AuditResponse, Classification, ConceptTag,
    GeographicScope, StageName, StageOutcome, StageResult, Verdict,
)
from audit.engine import NAMES, dump
from audit.provenance import build_base, with_prompts
from audit.schemas import RunDetail
from audit.search.base import SearchResult
from audit.search.fetch import DeadCandidate, FetchOutcome, LivePage
from audit.search.textmatch import directional_containments
from audit.workflow import classification, pipeline, related_removals, source, web_search
from audit.workflow.prompt_registry import DEFAULTS, validation_codes
from audit.workflow.relevance import RelevanceResult
from audit.workflow.similarity import SimilarCandidate, SimilarityEvidence


class DemoSeedRefused(ValueError):
    """The target is not a test database; nothing was written."""


OUTLETS = (
    ("Harbor Ledger", "harbor-ledger.example"),
    ("Kuzey Gazete", "kuzey-gazete.example"),
    ("Valley Courier", "valley-courier.example"),
)
SOURCE_HOST = "meridian-wire.example"
FICTION = "This is fictional demo data, and every event and participant is invented."
VERSION_NOTES = (
    "initial fictional demo baseline",
    "relevance prompt tightened",
    "relevance and web-search fictional wording clarified",
)

# Labels describe fictional history, not database version numbers: an existing
# API version means the appended demo versions receive higher numbers.
PROMPT_CHANGES = (
    ("relevance_v2", "relevance", "Focus on the invented public spending example.", "demo-editor-1"),
    ("relevance_v3", "relevance", "Clarify the invented accountability example.", "demo-editor-2"),
    ("web_search_v2", "web_search", "Compare the invented independent copies carefully.", "demo-editor-1"),
)
STEP_MODELS = {
    "classification": "demo-model-small", "relevance": "demo-model-small",
    "related_removals": "demo-model-small", "source": "demo-model-large",
    "web_search": "demo-model-large",
}


@dataclass(frozen=True)
class Case:
    slug: str
    title: str
    story: str
    expected: Verdict
    # Deliberate historical misses; the newest version still has one regression.
    predictions: tuple[str, str, str]


CASES = (
    Case("reservoir", "Fictional council requests reservoir contract records",
         "The council of the fictional town of Loomhaven requested reservoir invoices. "
         "Its fictional mayor asked a review panel to explain missing inspection certificates. "
         "Residents requested a public timetable for the review.",
         Verdict.POSSIBLE_CENSORSHIP, ("censorship", "censorship", "censorship")),
    Case("library", "Fictional library funding report moves to a new section",
         "The fictional town of Reedmere published a library funding report. "
         "A council clerk said the reading room would receive new shelves. "
         "The fictional publisher reorganized its community section after the report appeared.",
         Verdict.MOVED, ("moved", "moved", "moved")),
    Case("tender", "Fictional workshop tender returns in a revised report",
         "The fictional company Lanternwheel Works submitted a workshop tender. "
         "A fictional procurement panel published the scoring sheets for public inspection. "
         "The publisher later revised the report while keeping its central tender figures.",
         Verdict.LIKELY_MOVED, ("likely_moved", "inconclusive", "likely_moved")),
    Case("ferry", "Independent outlets retain fictional ferry safety report",
         "Inspectors in the fictional district of Cloudquay reviewed ferry safety logs. "
         "The fictional operator Paperwake Transit agreed to publish maintenance schedules. "
         "Several invented outlets carried the same account of the inspection meeting.",
         Verdict.FOUND_ONLINE, ("inconclusive", "found", "found")),
    Case("garden", "Fictional public garden budget remains at its original address",
         "The fictional town of Fernwick published a public garden budget. "
         "Its fictional treasurer listed the cost of paths and accessible seating. "
         "A residents panel asked for monthly progress statements.",
         Verdict.NOT_REMOVED, ("inconclusive", "inconclusive", "not_removed")),
    Case("ribbons", "A fictional weekend column compares ribbon colors",
         "A fictional craft column in Pebblewisp compared ribbon colors for gift boxes. "
         "Its invented contributor suggested soft loops and reusable wrapping. "
         "The column made no claims about public officials, safety or public spending.",
         Verdict.LIKELY_LOW_PUBLIC_INTEREST, ("inconclusive", "low_interest", "inconclusive")),
)


def _date(day: int, hour: int = 10, minute: int = 0) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:{minute:02d}:00+00:00"


def _prompt_sets(changes: dict[str, str]) -> dict[int, dict[str, str]]:
    return {1: dict(DEFAULTS), 2: {**DEFAULTS, "relevance": changes["relevance_v2"]},
            3: {**DEFAULTS, "relevance": changes["relevance_v3"],
                "web_search": changes["web_search_v2"]}}


def _snapshot(version: int, prompt_texts: dict[str, str]) -> dict:
    note = VERSION_NOTES[version - 1]
    base = build_base({
        "model_name": f"demo-model-{version}", "model_enabled": True, "model_steps": STEP_MODELS,
        "search_provider": "searxng", "searxng_url": "https://search.demo.test",
        "archive_base_url": "https://archive.demo.test", "temperature": 0,
    })
    base.update({
        "sources": {"DEMO_README.txt": f"Fictional demo version {version}: {note}.\n",
                    "workflow/prompts.py": f"# Fabricated excerpt for comparison only\n# {note}\n"},
        "dependencies": {}, "demo_note": note,
    })
    snapshot = with_prompts(base, prompt_texts)
    # Demo-owned IDs are fixed fictional labels, deliberately not fingerprints.
    snapshot.update(id=f"wf_demo_v{version}", created_at=f"2026-10-{19 + version}T09:00:00+00:00")
    return snapshot


def _article(slug: str, title: str, story: str, *, cited: bool = False) -> Article:
    content = f"{FICTION} {story}"
    if cited:
        content += (f' The fictional Meridian Wire supplied this account at '
                    f'<a href="https://{SOURCE_HOST}/reports/{slug}">Meridian Wire</a>.')
    return Article(url=f"https://{OUTLETS[0][1]}/reports/{slug}", title=title,
                   content=content, published_at="2026-10-25T08:00:00+00:00")


def _model_call(prompt: str, user: str, result, model: str, *, failed: bool = False,
                max_tokens: int = 400) -> dict:
    result_type = type(result)
    parsed = dump(result)
    request = {"model": model, "messages": [{"role": "system", "content": prompt},
                                            {"role": "user", "content": user}],
               "max_tokens": max_tokens, "temperature": 0}
    if result_type is not str:
        request["response_format"] = {"type": "json_schema", "json_schema": {
            "name": result_type.__name__, "schema": result_type.model_json_schema()}}
    usage = {"prompt_tokens": 180, "completion_tokens": 45, "total_tokens": 225}
    return {"type": "model", "request": request,
            "raw_completion": {"id": "completion_demo", "object": "chat.completion",
                               "created": 1793527200, "model": model,
                               "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                   "role": "assistant", "content": "{invalid demo JSON" if failed
                                   else parsed if isinstance(parsed, str) else json.dumps(parsed)}}],
                               "usage": usage},
            "parsed_output": None if failed else parsed, "response_model": model,
            "usage": usage, "error_type": "ValidationError" if failed else None}


def _archive(path: str, output, params=None) -> dict:
    return {"type": "archive_get", "path": path, "params": params,
            "output": dump(output), "status": 200, "error_type": None}


def _fetch(kind: str, args: list, output, **kwargs) -> dict:
    return {"type": kind, "input": dump({"args": args, "kwargs": kwargs}),
            "output": dump(output), "error_type": None}


@dataclass
class _Trace:
    article: Article
    prompts: dict[str, str]
    models: dict[str, str]
    steps: list = field(default_factory=list)
    stages: list = field(default_factory=list)
    state: dict = field(default_factory=dict)

    def add(self, result: StageResult, *, calls=(), evidence=(), extra=None):
        name = result.stage.value
        assert name == NAMES[len(self.steps)]
        inp = {"article": dump(self.article), "prior_stages": dump(self.stages), **dump(self.state)}
        if extra:
            inp.update(extra)
        status = ("skipped" if result.skipped else "failed" if
                  result.explanation.startswith("Stage did not complete") else "completed")
        self.steps.append(dump({"name": name, "status": status, "input": inp, "output": result,
                                "model": calls[0]["request"]["model"] if calls else None,
                                "model_calls": list(calls), "evidence": list(evidence),
                                "duration_ms": 0 if result.skipped else 240 + len(self.steps) * 37}))
        self.stages.append(result)

    def skip_rest(self, reason: str):
        for name in NAMES[len(self.steps):]:
            self.add(pipeline._skipped(StageName(name), reason))

    def call(self, prompt_name, result, *, failed=False, user=None, max_tokens=400):
        return _model_call(self.prompts[prompt_name], user or f"DEMO article: {self.article.title}\n{self.article.content}",
                           result, self.models[NAMES[len(self.steps)]], failed=failed, max_tokens=max_tokens)


def _classification(t: _Trace, *, skipped=False):
    if skipped:
        t.add(pipeline._skipped(StageName.CLASSIFICATION, "no IPTC concept catalog"))
        t.state["classification"] = None
        return
    tags = [ConceptTag(notation="medtop:20000657", label="local government"),
            ConceptTag(notation="medtop:20000350", label="public finance")]
    cls = Classification(subject_tags=tags, genre=ConceptTag(notation="genre:Current", label="Current"),
                         geographic_scope=GeographicScope.LOCAL,
                         gist=f"Fictional demo account: {t.article.title}.")
    coarse = classification.CoarseClassification(top_level_topics=["medtop:11000000", "medtop:04000000"],
                genre="genre:Current", geographic_scope="local", gist=cls.gist)
    fine = classification.FineClassification(topics=[x.notation for x in tags], confidences=[0.94, 0.88])
    roots = [Concept(notation=n, scheme="iptc_mediatopic", pref_label=label)
             for n, label in zip(coarse.top_level_topics, ("politics", "economy"))]
    catalog_topics = roots + [Concept(notation=x.notation, scheme="iptc_mediatopic", pref_label=x.label,
                                      parent_notation=parent.notation)
                              for x, parent in zip(tags, roots)]
    catalog_genre = Concept(notation=cls.genre.notation, scheme="iptc_genre", pref_label=cls.genre.label)
    catalog = {"locale": "en-GB", "fetched_at": "2026-10-25T07:00:00+00:00",
               "topics": dump(catalog_topics), "genres": [dump(catalog_genre)]}
    out = classification.ClassificationOutcome(classification=cls,
        explanation="Two catalog-grounded topics, current reporting and local scope (fictional demo).",
        details={**dump(cls), "coarse_top_levels": coarse.top_level_topics, "fine_menu_size": 2,
                 "fine_failed": False, "dropped": [], "collapsed": [], "siblings_folded": [],
                 "roots_dropped": [], "vague_dropped": [], "low_confidence_dropped": []})
    t.add(StageResult(stage=StageName.CLASSIFICATION, outcome=StageOutcome.PASS,
                      explanation=out.explanation, details=out.details),
          calls=[t.call("classification_coarse", coarse, max_tokens=350),
                 t.call("classification_fine", fine, max_tokens=300)],
          evidence=[_archive("/concepts", {"concepts": dump(catalog_topics)},
                             {"scheme": "iptc_mediatopic", "locale": "en-GB"}),
                    _archive("/concepts", {"concepts": [dump(catalog_genre)]},
                             {"scheme": "iptc_genre", "locale": "en-GB"})],
          extra={"catalog": catalog})
    t.state["classification"] = cls


def _similarity(t: _Trace, mode: str):
    moved = mode == "moved"
    hit = SearchResult(url=t.article.url.replace("/reports/", "/community/"),
                       title=t.article.title, snippet="Fictional archive candidate.", source="archive")
    text = t.article.content if moved else (
        FICTION + " An invented pottery class opens a bright studio where hobby groups can shape "
        "clay bowls and learn decorative patterns beside a display of handmade cups. "
        "The fictional tutor recommends broad brushes and describes how different glazes change "
        "the appearance of a bowl after it has cooled on a shelf. "
        "Participants in this imagined workshop may take their finished pieces home in reusable "
        "boxes, while spare tools remain available for the following weekend session.")
    page = LivePage(url=hit.url, final_url=hit.url, status=200, title=hit.title, text=text)
    old, new = directional_containments(t.article.content, text)
    out = SimilarityEvidence(available=True,
        reason="Measured complete republication." if moved else "Archive candidate is a different article.",
        candidates=[SimilarCandidate(url=hit.url, title=hit.title, score=0.93,
                    same_publisher=True, status="live", final_url=hit.url,
                    old_in_new=old, new_in_old=new, fit="verbatim" if moved else "sibling")],
        relocated_to=hit if moved else None, relocation_old_in_new=old if moved else None,
        relocation_new_in_old=new if moved else None, hits=[hit], fetched=FetchOutcome(live=[page]))
    t.add(StageResult(stage=StageName.SIMILARITY,
                      outcome=StageOutcome.PASS if moved else StageOutcome.INCONCLUSIVE,
                      explanation=out.reason, details=pipeline._similarity_details(out)),
          evidence=[_archive("/articles/700/similar", {"articles": [{"link": hit.url,
                    "title": hit.title, "similarity_score": 0.93}]}, {"limit": 5, "date_range_days": 30}),
                    _fetch("fetch_candidates", [[hit]], out.fetched, exclude_url=t.article.url)])
    t.state["similarity"] = out
    return out


def _relevance(t: _Trace, relevant: bool, *, failed=False):
    out = RelevanceResult(is_relevant=relevant, confidence=0.93,
        reasoning="Fictional public spending warrants scrutiny." if relevant else
                  "The fictional leisure item has no independent accountability angle.")
    result = pipeline._stage_failed(StageName.RELEVANCE) if failed else StageResult(
        stage=StageName.RELEVANCE, outcome=StageOutcome.PASS if relevant else StageOutcome.FAIL,
        confidence=out.confidence, explanation=out.reasoning)
    t.add(result, calls=[t.call("relevance", out, failed=failed)])
    if not failed:
        t.state["relevance"] = out
    return out


def _related(t: _Trace, pattern: bool, relevant: bool):
    anchor = int(datetime.fromisoformat(_date(5)).timestamp())
    candidates = [related_removals.RelatedRemoval(
        audit_id=710 + i, title=f"Fictional follow-up {i + 1}: {t.article.title}",
        url=f"https://{host}/reports/demo-followup-{i}", newspaper=name,
        created_at=anchor + i * 86400, publish_date=_date(1), status=0,
        shared_terms=["fictional procurement", "review"])
        for i, (name, host) in enumerate(OUTLETS)]
    members = candidates if pattern else []
    flag, publishers, censored = related_removals.decide_flag(members)
    out = related_removals.RelatedRemovalsEvidence(available=True, anchor_ts=anchor,
        anchor_source="first_dead_check", window_size=3, entities=["fictional procurement"],
        prefiltered=candidates, members=members, publishers=publishers, human_censored=censored,
        model_confirmed=True, flag=flag, kept_in_scope=flag and not relevant,
        reason=f"Fictional window: model confirmed {len(members)} of 3 candidates.")
    archive_rows = [{"id": m.audit_id, "created_at": m.created_at, "status": 0, "decision": None,
                     "article": {"link": m.url, "title": m.title, "newspaper": m.newspaper,
                                 "publish_date": m.publish_date}} for m in candidates]
    t.add(StageResult(stage=StageName.RELATED_REMOVALS,
                      outcome=StageOutcome.PASS if flag else StageOutcome.FAIL,
                      explanation=out.reason, details=pipeline._related_details(out)),
          calls=[t.call("related_removals", "1, 2, 3" if pattern else "NONE",
                        user=related_removals.build_prompt(t.article, candidates,
                             gist=t.state["classification"].gist if t.state["classification"] else "",
                             publish_date="2026-10-25", text=t.article.content), max_tokens=40)],
          evidence=[_archive("/audits", {"items": archive_rows},
                    {"view": "public", "status": 0, "date_from": "2026-10-22",
                     "date_to": "2026-11-19", "page": 1, "page_size": 100,
                     "sort_by": "created_at", "sort_order": "desc"})])
    t.state["related"] = out
    return out


def _source(t: _Trace, mode: str):
    intact, dead = mode == "not_removed", mode == "censorship"
    url = f"https://{SOURCE_HOST}/reports/{t.article.url.rsplit('/', 1)[-1]}"
    quote = "The fictional Meridian Wire supplied this account"
    raw = source.SourceResult(source_kind="other_outlet" if dead else "unknown",
                              source_name="Meridian Wire" if dead else "",
                              source_url=url if dead else "", attribution_quote=quote if dead else "")
    missing = DeadCandidate(url=url, title=t.article.title, snippet="Fictional cited account.",
                            status=404, reason="cited source — HTTP 404", source="cited") if dead else None
    out = source.SourceEvidence(self_status="live_intact" if intact else "dead_or_unreachable",
        self_containment=1.0 if intact else None, live_at_original_url=intact,
        **dump(raw), source_host=SOURCE_HOST if dead else "", source_status="dead" if dead else "n/a",
        dead_source=missing, extracted_links=[url] if dead else [])
    out.reasoning = source.describe(out)
    page = LivePage(url=t.article.url, final_url=t.article.url, status=200,
                    title=t.article.title, text=t.article.content) if intact else None
    events = [_fetch("probe_removed_url", [t.article.url, t.article.title], page)]
    if dead:
        hit = SearchResult(url=url, title=t.article.title, snippet=quote, source="cited")
        events.append(_fetch("fetch_candidates", [[hit]], FetchOutcome(dead=[missing]),
                             exclude_url=t.article.url))
    t.add(StageResult(stage=StageName.SOURCE,
                      outcome=StageOutcome.PASS if intact or dead else StageOutcome.INCONCLUSIVE,
                      explanation=out.reasoning, details=pipeline._source_details(out)),
          calls=[t.call("source", raw)], evidence=events)
    t.state["source"] = out
    return out


def _web(t: _Trace, mode: str, *, failed=False):
    host = OUTLETS[0][1] if mode == "likely_moved" else OUTLETS[1][1]
    hit = SearchResult(url=f"https://{host}/updates/demo-copy", title=t.article.title,
                       snippet="Fictional search result.", source="web")
    text = t.article.content
    if mode == "likely_moved":
        text = " ".join(text.split()[:int(len(text.split()) * 0.75)]) + " A fictional editor added context."
    elif failed:
        text += (" The fictional editor requested copies of every workshop inspection certificate. "
                 "An invented residents panel asked how each tender score had been calculated. "
                 "The fictional clerk promised to display the records in the community reading room.")
    elif mode != "found":
        text = (FICTION + " An invented bakery offers seed cakes with small paper decorations and "
                "asks hobby groups to choose a different arrangement for each display tray. "
                "The fictional baker describes a leisurely afternoon of mixing dough, folding "
                "wrappers and preparing plain recipe cards for visitors to take home. "
                "This imagined feature ends with suggestions for arranging a tea table beside "
                "a shelf of painted cups and sharing the leftover wrapping paper.")
    page = LivePage(url=hit.url, final_url=hit.url, status=200, title=hit.title, text=text)
    fresh = FetchOutcome(live=[page])
    # The engine reuses measured archive candidates rather than fetching them twice.
    sim = t.state["similarity"]
    fetched = sim.fetched.model_copy(deep=True)
    fetched.live.append(page)
    raw = web_search.WebSearchResult(matches=[web_search.PageMatch(url=hit.url)]
            if mode in ("likely_moved", "found") else [],
            reasoning="Fictional same-story copy confirmed." if mode in ("likely_moved", "found")
            else "The fictional page does not reproduce this story.")
    out = web_search._ground(raw, fetched, t.article, len(sim.hits) + 1)
    src = t.state["source"]
    if src.dead_source:
        out.dead_same_story.insert(0, src.dead_source)
    calls = [t.call("web_search", raw, failed=failed,
                    user=web_search._build_prompt(t.article, fetched, pages, chars), max_tokens=700)
             for pages, chars in (web_search._PROMPT_BUDGETS if failed else [(3, 500)])]
    result = pipeline._stage_failed(StageName.WEB_SEARCH) if failed else StageResult(
        stage=StageName.WEB_SEARCH, outcome=StageOutcome.PASS if out.matches else StageOutcome.FAIL,
        explanation=out.reasoning, details=pipeline._web_details(out))
    t.add(result, calls=calls, evidence=[
        {"type": "search", "query": t.article.title, "max_results": 5,
         "language": None, "results": [dump(hit)], "error_type": None},
        _fetch("fetch_candidates", [[hit]], fresh, exclude_url=t.article.url)])
    return out


def _response(t: _Trace, web, *, short=None) -> AuditResponse:
    verdict, findings = pipeline._decide(web)
    flags = []
    src, rr = t.state.get("source"), t.state.get("related")
    if src and src.dead_source:
        flags.append(AuditFlag.SOURCE_DEAD)
        findings += (f" RED FLAG (SOURCE_DEAD): the source this article itself credits"
                     f" ({src.source_name}), {src.dead_source.url}, answers "
                     f"{src.dead_source.reason.replace('cited source — ', '')} — the origin copy of the "
                     "story is gone too. This is a pointer for a human reviewer, not a finding of censorship.")
    if rr and rr.flag:
        flags.insert(0, AuditFlag.RELATED_REMOVALS)
        findings += " " + related_removals.describe(rr)
        if rr.kept_in_scope:
            findings += (" On its own the public-interest screen judged this article out of scope "
                         f"({t.state['relevance'].reasoning}); the pattern of related removals kept it in the audit.")
    cls = t.state.get("classification")
    return AuditResponse(verdict=verdict, findings=pipeline._with_classification(findings, cls),
                         stages=t.stages, classification=cls, flags=flags, short_circuited_at=short)


def _execute(article: Article, mode: str, prompt_texts: dict[str, str]):
    t = _Trace(article, prompt_texts, STEP_MODELS)
    if mode == "queued":
        return t, None
    if mode == "disabled":
        t.add(pipeline._stage_failed(StageName.CLASSIFICATION))
        t.skip_rest("model is disabled; audit did not run")
        return t, pipeline._audit_failed("Model is disabled; the audit did not run.",
                                         stages=t.stages, classification=None)
    _classification(t, skipped=mode in ("relevance_failure", "web_failure"))
    sim = _similarity(t, mode)
    if mode == "moved":
        t.skip_rest("similarity found a measured relocation")
        web = web_search.WebSearchEvidence(relocated_to=sim.relocated_to, matches=[sim.relocated_to],
            relocation_old_in_new=sim.relocation_old_in_new, relocation_new_in_old=sim.relocation_new_in_old,
            hits_searched=len(sim.hits), live_pages_read=len(sim.fetched.live), reasoning=sim.reason)
        return t, _response(t, web, short=StageName.SIMILARITY)
    relevant = mode not in ("low_interest", "kept_in_scope")
    rel = _relevance(t, relevant, failed=mode == "relevance_failure")
    if mode in ("running", "interrupted", "cancelled"):
        return t, None
    if mode == "relevance_failure":
        t.skip_rest("relevance did not complete")
        return t, pipeline._audit_failed("The relevance model failed.", stages=t.stages, classification=None)
    rr = _related(t, mode in ("censorship", "kept_in_scope"), relevant)
    if mode == "low_interest":
        t.skip_rest("relevance failed")
        cls = t.state["classification"]
        findings = f"Removed article judged not of public interest: {rel.reasoning} " + related_removals.describe(rr)
        return t, AuditResponse(verdict=Verdict.LIKELY_LOW_PUBLIC_INTEREST, stages=t.stages,
            short_circuited_at=StageName.RELEVANCE, findings=pipeline._with_classification(findings, cls),
            classification=cls)
    src = _source(t, "censorship" if mode == "source_dead_only" else mode)
    if src.live_at_original_url:
        t.skip_rest("the removed URL is live with its text substantially intact")
        web = web_search.WebSearchEvidence(live_at_original_url=True, original_url=article.url,
                                           original_url_containment=src.self_containment)
    else:
        web = _web(t, mode, failed=mode == "web_failure")
    if mode == "web_failure":
        return t, pipeline._audit_failed("The web-search model failed on every attempt.",
                                         stages=t.stages, classification=t.state["classification"])
    return t, _response(t, web)


def _run(slug, title, story, mode, *, prompt_texts: dict[str, str], version=3, day=16, hour=10, expected=None,
         category="benchmark", origin="benchmark", article_slug=None) -> Run:
    article = _article(article_slug or slug, title, story, cited=mode in ("censorship", "source_dead_only"))
    trace, response = _execute(article, mode, prompt_texts)
    pending = mode in ("queued", "running")
    failed = mode == "interrupted" or (response and response.verdict == Verdict.AUDIT_FAILED)
    status = mode if pending or mode == "cancelled" else "failed" if failed else "succeeded"
    code = mode if mode in ("interrupted", "cancelled") else "audit_failed"
    return Run(id=f"run_demo_{slug}", workflow_id=f"wf_demo_v{version}",
        category=category, origin=origin, status=status, created_at=_date(day, hour),
        finished_at=None if pending else _date(day, hour, 2), label=f"DEMO · {title}",
        article_title=title, verdict=response.verdict.value if response else None,
        synthetic=mode == "disabled", input=dump(AuditRequest(removed_article=article)),
        output=dump(response), steps=trace.steps,
        model={"name": f"demo-model-{version}", "enabled": mode != "disabled"},
        error={"code": code, "message": ERRORS[code]} if failed or mode == "cancelled" else None,
        expected_verdict=expected.value if expected else None)


def _runs(prompt_sets: dict[int, dict[str, str]]) -> list[Run]:
    rows = [_run(f"{case.slug}_v{v}", case.title, case.story, case.predictions[v - 1],
                 version=v, prompt_texts=prompt_sets[v], day=v * 3, hour=9 + i, expected=case.expected, article_slug=case.slug)
            for v in (1, 2, 3) for i, case in enumerate(CASES)]
    scenarios = (
        ("kept", "Fictional craft fair stays in scope after related removals", "kept_in_scope"),
        ("relevance_failure", "Fictional relevance analysis returns invalid JSON", "relevance_failure"),
        ("web_failure", "Fictional search analysis exhausts its retry ladder", "web_failure"),
        ("disabled", "Fictional audit with the model disabled", "disabled"),
        ("stopped", "Fictional audit stopped after relevance", "interrupted"),
        ("running", "Fictional audit currently checking related removals", "running"),
        ("queued", "Fictional audit waiting for an available worker", "queued"),
        ("live_api", "Fictional safety report survives at an independent outlet", "found"),
        ("live_webhook", "Fictional contract report has a dead cited source", "source_dead_only"),
        ("cancelled", "Fictional audit stopped by a person", "cancelled"),
    )
    for i, (slug, title, mode) in enumerate(scenarios):
        story = ("A fictional club in the invented town of Loomhaven announced a craft fair. "
                 "Its fictional treasurer described plans for donated ribbons. "
                 "Several invented outlets covered the same fair.") if mode == "kept_in_scope" else CASES[i % 6].story
        if slug in ("live_api", "live_webhook"):
            story = CASES[3 if slug == "live_api" else 0].story
        rows.append(_run(slug, title, story, mode, prompt_texts=prompt_sets[3], day=17 if mode in ("queued", "running") else 16,
                         hour=11 if mode == "queued" else 10 if mode == "running" else 9 + i,
                         category="live" if slug.startswith("live_") else "benchmark",
                         origin="api" if slug == "live_api" else "webhook" if slug == "live_webhook" else "benchmark"))
    return rows


def _reactions(runs: list[Run]) -> list[Reaction]:
    choices = (
        ("reservoir_v3", "needs_review", None, "The fictional dead source warrants a closer comparison."),
        ("library_v3", "agree", None, "The invented copy contains all of the archived text."),
        ("tender_v2", "disagree", Verdict.LIKELY_MOVED, "The fictional same-publisher copy deserves a partial-fit review."),
        ("ferry_v3", "agree", None, "The independent copy preserves the fictional inspection account."),
        ("garden_v1", "disagree", Verdict.NOT_REMOVED, "The fictional original should be checked again for intact text."),
        ("ribbons_v3", "disagree", Verdict.LIKELY_LOW_PUBLIC_INTEREST, "The invented ribbon column appears outside public-interest scope."),
        ("kept", "needs_review", None, "Compare the fictional removal dates before drawing a conclusion."),
        ("live_webhook", "needs_review", None, "A fictional missing source alone does not establish why the page vanished."),
    )
    by_id = {r.id: r for r in runs}
    return [Reaction(id=f"rx_demo_{i + 1}", run_id=f"run_demo_{slug}",
        workflow_id=by_id[f"run_demo_{slug}"].workflow_id, reaction=reaction,
        reviewer=("demo-reviewer-1", "demo-reviewer-2", "demo-reviewer-3")[i % 3],
        note=note, expected_verdict=expected.value if expected else None,
        created_at=_date(17, 9, i)) for i, (slug, reaction, expected, note) in enumerate(choices)]


PREFIXES = ((Workflow, "wf_demo_"), (Run, "run_demo_"), (Reaction, "rx_demo_"),
            (PromptVersion, "pv_demo_"), (PromptActivation, "pa_demo_"))


def _demo_id(model, prefix: str):
    # Literal, case-sensitive prefix: `startswith`/LIKE would treat `_` as a wildcard.
    return func.substr(model.id, 1, len(prefix)) == prefix


def is_test_database(path: Path) -> bool:
    """A test database is named `*-test.sqlite3` (test mode) or lives under `data/demo/`.
    The resolved path counts, so a test-named symlink to the live database is refused. The rule
    lives in config so live mode refuses exactly the databases the seeder may write."""
    return is_test_database_path(path)


def _has_demo_marker(path: Path) -> bool:
    # Inspection must not write: `mode=ro` can still create WAL sidecars, `immutable=1` cannot.
    # Immutable reads skip the WAL and hot-journal recovery, either of which may add or remove
    # the marker. Only a quiescent main file (no non-empty -wal or -journal) can authorise by
    # its marker: fail closed.
    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.is_file() and sidecar.stat().st_size > 0:
            return False
    return has_demo_marker(path)


def _preflight(database_url: str) -> str:
    """Refuse anything but a test database before writing a byte, and return the URL to open:
    the canonical path that was checked, so `..` after a symlink cannot open another file.
    Non-demo rows in a test database (the API's own workflow, runs submitted in test mode)
    are kept, never touched."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise DemoSeedRefused("Demo data requires a separate SQLite database.")
    if url.query.get("uri") or (url.database or "").startswith("file:"):
        raise DemoSeedRefused("Use a plain SQLite path or :memory:, without SQLite URI options.")
    if not url.database or url.database == ":memory:":
        return database_url
    path = Path(url.database).resolve()
    checked = f"sqlite:///{path}"
    # Store reparses the URL: a resolved name with `?` or `#` would open a different file.
    if make_url(checked).database != str(path) or make_url(checked).query:
        raise DemoSeedRefused("The resolved database path cannot be opened unambiguously.")
    if is_test_database(path) or (path.is_file() and _has_demo_marker(path)):
        return checked
    raise DemoSeedRefused("Not a test database: use a file named *-test.sqlite3, a path under "
                          "data/demo/, or a database that already holds demo data.")


def _demo_article_metadata(session, article, values, source, removed_at):
    # The URL was matched to a demo case. Only demo-owned rows may be repaired.
    non_demo = session.scalar(select(Run.id).where(
        Run.article_ref == article.ref, ~_demo_id(Run, "run_demo_")).limit(1))
    if non_demo is not None:
        return
    if article.ref != values["ref"] and session.scalar(select(Run.id).where(
            Run.article_ref == article.ref).limit(1)) is not None:
        return
    article.origin = "demo"
    article.source = source
    article.removed_at = removed_at
    article.published_at = values["published_at"]


def _seed_prompt_history(session: Session) -> dict[int, dict[str, str]]:
    """Append demo history once; never rewrite versions or reorder activations.

    Defaults were synced by the store. Use its numbering/hash/activation invariants
    inside the demo transaction, with fixed IDs and fictional historical timestamps.
    The demo history never changes the active prompt selection: its activations are
    appended all-or-nothing and end by restoring the selection observed after the
    standard default sync (which every API startup runs before seeding anyway).
    """
    changes = {}
    for key, name, wording, author in PROMPT_CHANGES:
        row = session.get(PromptVersion, f"pv_demo_{key}")
        if row is None:
            text = DEFAULTS[name] + f"\n\nDemo wording change (fictional): {wording}\n"
            if validation_codes(name, text):
                raise DemoSeedRefused("Fictional demo prompt failed registry validation.")
            if session.scalar(select(PromptVersion.id).where(
                    PromptVersion.name == name, PromptVersion.text == text)):
                raise DemoSeedRefused("Fictional demo prompt already exists under a non-demo ID.")
            number = session.scalar(select(func.max(PromptVersion.version)).where(
                PromptVersion.name == name)) or 0
            row = PromptVersion(id=f"pv_demo_{key}", name=name, version=number + 1,
                text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), origin="api",
                author=author, note="Fictional demo change; not a source default.",
                created_at=f"2026-10-{21 if key == 'relevance_v2' else 22}T07:00:00+00:00")
            session.add(row)
            session.flush()
        changes[key] = row.text

    # Invariant: the demo history never changes which prompt version is active. `seed()`
    # first runs the store's standard default sync, exactly as every API startup does
    # before seeding (with prompt versions, an updated source default replaces an active default);
    # the selection observed after that sync is what the demo history preserves. The
    # demo activation story is appended all-or-nothing: only when no demo activation
    # exists yet, ending with activations that restore that selection (the source
    # defaults on a fresh database, an operator's choice on an upgraded one). Once any
    # demo activation exists, none is added again, even if some were removed.
    demo_activation = func.substr(PromptActivation.id, 1, len("pa_demo_")) == "pa_demo_"
    if session.scalar(select(PromptActivation.id).where(demo_activation).limit(1)) is None:
        restore = {name: session.scalar(select(PromptActivation.version_id).where(
            PromptActivation.name == name).order_by(ACTIVATION_ORDER).limit(1))
            for name in ("relevance", "web_search")}
        story = (
            ("relevance_v2", "relevance", "pv_demo_relevance_v2", "2026-10-21T08:00:00+00:00"),
            ("web_search_v2", "web_search", "pv_demo_web_search_v2", "2026-10-22T08:00:00+00:00"),
            ("relevance_v3", "relevance", "pv_demo_relevance_v3", "2026-10-22T08:30:00+00:00"),
            ("relevance_default", "relevance", None, "2026-11-18T08:00:00+00:00"),
            ("web_search_default", "web_search", None, "2026-11-18T08:30:00+00:00"),
        )
        for key, name, version_id, stamp in story:
            note = "Activated a fictional demo wording change for a fictional benchmark."
            if version_id is None:
                default_id = session.scalar(select(PromptVersion.id).where(
                    PromptVersion.name == name, PromptVersion.origin == "default",
                    PromptVersion.text == DEFAULTS[name]))
                version_id = restore[name] or default_id
                note = ("Back to the source default after a fictional benchmark." if version_id == default_id
                        else "Restored the selection that was active when the demo history was added.")
            session.add(PromptActivation(id=f"pa_demo_{key}", name=name, version_id=version_id,
                kind="activate", author="demo-editor-2" if "v3" in key else "demo-editor-1",
                note=note, created_at=stamp))
            # Active selection uses SQLite rowid, not the fictional timestamps or IDs.
            session.flush()
    return _prompt_sets(changes)


def seed(database_url: str) -> dict:
    """Merge fixed fictional records atomically; return table, status and verdict counts.

    Call after API startup has interrupted incomplete work, to retain the demo queue.
    """
    checked_url = _preflight(database_url)
    store = Store(checked_url)
    try:
        store.sync_prompt_defaults(DEFAULTS)
        with Session(store.engine) as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            prompt_sets = _seed_prompt_history(session)
            runs = _runs(prompt_sets)
            reactions = _reactions(runs)
            for version in (1, 2, 3):
                snapshot = _snapshot(version, prompt_sets[version])
                session.merge(Workflow(id=snapshot["id"], snapshot=snapshot))
            session.flush()
            for row in runs:
                values = article_values(row.input["removed_article"], row.category, "demo", row.created_at)
                article = session.scalar(select(ArticleRow).where(ArticleRow.url == values["url"]))
                if article is None:
                    article = ArticleRow(**values, source=OUTLETS[0][0], removed_at=_date(2))
                    session.add(article)
                    session.flush()
                _demo_article_metadata(session, article, values, OUTLETS[0][0], _date(2))
                row.article_ref = article.ref
                session.merge(row)
            for index, title in enumerate(("Fictional harbor dredging permit questions",
                                            "Fictional school meal supplier change",
                                            "Fictional bridge inspection summary")):
                outlet, host = OUTLETS[index]
                values = article_values({"url": f"https://{host}/demo/unaudited-{index + 1}",
                    "title": title, "content": FICTION + " A fictional clerk requested the invented public records.",
                    "published_at": _date(12 + index)}, "benchmark", "demo", _date(15))
                article = session.scalar(select(ArticleRow).where(ArticleRow.url == values["url"]))
                if article is None:
                    session.add(ArticleRow(**values, source=outlet, removed_at=_date(16)))
                else:
                    _demo_article_metadata(session, article, values, outlet, _date(16))
            session.flush()
            for row in reactions:
                session.merge(row)
            session.flush()
            # Counts cover demo rows only; other rows in a test database are not ours.
            tables = {model.__tablename__: len(session.scalars(
                select(model.id).where(_demo_id(model, prefix))).all()) for model, prefix in PREFIXES}
            tables["articles"] = session.scalar(select(func.count()).select_from(ArticleRow)
                                                .where(ArticleRow.origin == "demo"))
            demo_runs = session.execute(select(Run.status, Run.verdict).where(_demo_id(Run, "run_demo_"))).all()
            statuses = dict(sorted(Counter(status for status, _ in demo_runs).items()))
            verdicts = dict(sorted(Counter(v or "pending" for _, v in demo_runs).items()))
        for row in runs:
            RunDetail.model_validate(store.run(row.id))
        return {"tables": tables, "statuses": statuses, "verdicts": verdicts}
    finally:
        store.close()
