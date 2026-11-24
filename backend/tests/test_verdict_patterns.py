"""Invented page families covering relocation, boilerplate and pending rewrites."""

from html import escape
from types import SimpleNamespace

import httpx
import pytest

from audit.clients.models import SimilarArticle
from audit.domain import Article, AuditRequest
from audit.engine import AuditEngine
from audit.search import fetch as fetch_module
from audit.search.base import BaseSearchProvider, SearchResult
from audit.search.boilerplate import build_boilerplate, measure as measure_cleaned
from audit.search.fetch import FetchOutcome, LivePage, html_to_text
from audit.search.textmatch import MOVED_THRESHOLD, SIBLING_MAX, directional_containments
from audit.search.urls import is_public_web_url
from audit.workflow import classification, pipeline, relevance, source, web_search


OLD_URL = "https://dusulke.example/haber/71000/water-budget"
NEW_URL = "https://dusulke.example/haber/71001/water-budget-revised"
SECOND_URL = "https://dusulke.example/haber/71002/pottery-exhibition"
CITED_URL = "https://bulut-defteri.example/haber/82000/orchard-survey"

BUDGET = [
    "This invented report describes the water budget in the fictional district "
    "of Karaçam. On 12 March 2031 the council approved three storage tanks and "
    "a replacement pump for the northern reservoir. Members examined a schedule "
    "showing delivery dates, construction costs and expected household demand. "
    "The mayor said repairs would begin before the dry season, with temporary "
    "pipes keeping the surrounding streets supplied throughout the works.",
    "The finance committee allocated separate funds for equipment inspections "
    "and electricity purchases. Water tariffs will follow the quarterly "
    "inflation index, while households receiving assistance retain a discount. "
    "An opposition member requested monthly spending statements so residents "
    "can follow each contract. Engineers will publish pressure readings and "
    "explain why the hillside streets require additional pumping capacity.",
    "Residents asked whether the new tanks would change collection times at "
    "communal taps. The department promised written notices before any planned "
    "interruption and offered an evening telephone service for questions. "
    "Council clerks recorded the vote and placed the approved tables in the "
    "public reading room. A second meeting in June will review delivery "
    "progress and hear observations from the neighbourhood committees. "
    "Garden volunteers requested a separate illustrated leaflet about seedlings.",
]

REWRITE = [
    "Karaçam will expand drinking supplies following a decision taken on "
    "12 March 2031. Elected representatives authorised a trio of containers "
    "beside the upper basin, together with machinery to lift water uphill. "
    "Work crews are due to arrive ahead of summer; a bypass arrangement "
    "should maintain connections while installation proceeds.",
    "Charges for customers are to track changes in consumer prices every "
    "quarter. Reduced bills remain available to families on support. "
    "During deliberations a minority councillor sought regular financial "
    "disclosures. Technical staff intend to release measurements explaining "
    "how the higher neighbourhoods will benefit from stronger circulation.",
    "At the session, inhabitants sought reassurance about access to shared "
    "standpipes. Officials undertook to advertise shutdowns in advance and "
    "answer calls after working hours. Documentation is available for "
    "inspection, and representatives intend to revisit implementation in "
    "June 2031. The headline topic remains the district water budget.",
]

ORCHARD = [
    "This invented feature follows orchard volunteers mapping rare pear "
    "varieties on a distant plateau. Their notebooks record blossom shapes "
    "and the scent of each fruit, with pencil sketches beside weather "
    "observations. No water project or municipal finance decision appears "
    "in the survey, which concerns seasonal gardening techniques.",
    "A nursery keeper demonstrates how to wrap young branches against frost. "
    "Visitors taste stored fruit and compare the texture of dried slices. "
    "The workshop ends with instructions for cleaning wooden boxes and "
    "sorting seeds by size before the spring planting session.",
    "Children assemble paper labels for the collection and choose symbols "
    "representing sweet, sharp and mild flavours. The organisers plan "
    "another exhibition when the harvest finishes. Garden volunteers "
    "requested a separate illustrated leaflet about seedlings.",
]

FOOTER = [
    "Subscribe to our invented daily reading service for an uninterrupted "
    "selection of features, interviews, recipes and local notices. Members "
    "receive the morning digest, can save favourite columns and can adjust "
    "delivery preferences from their account page. The subscription panel "
    "also explains renewal dates, notification choices and accessible "
    "reading formats for visitors using smaller screens.",
    "Our fictional editorial desk welcomes general feedback through the "
    "reader form. Browse the publication guide for corrections, licensing "
    "terms, archive navigation and information about the weekly newsletter. "
    "Use the help menu to manage saved pages, update display settings or "
    "learn how the reading list works across devices. Thank you for "
    "supporting independent storytelling and returning to explore tomorrow.",
]

POTTERY = [
    "This invented exhibition presents ceramic bowls beside colourful woven "
    "baskets. Students paint small clay towers while instructors demonstrate "
    "glaze mixing techniques. Visitors compare textures under soft lamps and "
    "arrange decorated tiles around miniature sculptures before the workshop "
    "closes. A printed catalogue describes kiln temperatures and drying times.",
    "The fictional studio will open again next month with a second exhibition "
    "of painted vases and carved wooden frames. Teachers plan an evening class on "
    "simple wheel throwing, and children may bring their own sketches to turn "
    "into small glazed plates that the studio fires during the following week.",
]


def page(paragraphs):
    assert all(len(p) >= 40 for p in paragraphs)
    return (
        "<html><body>"
        + "".join("<p>" + escape(p) + "</p>" for p in paragraphs)
        + "</body></html>"
    )


def budget_article():
    return Article(
        url=OLD_URL,
        title="Karaçam water budget approved on 12 March 2031",
        content=page(BUDGET),
    )


def shortened_budget():
    paragraphs = list(BUDGET)
    paragraphs[-1] = paragraphs[-1].replace(
        "Garden volunteers requested a separate illustrated leaflet about seedlings.",
        "The revised version ends with an editor note about the next session.",
    )
    paragraphs[-1] = paragraphs[-1].replace("reading room", "reference room")
    return page(paragraphs)


class FakeAgent:
    def __init__(self, data):
        self.data = data

    async def run(self, prompt, model_settings=None):
        return SimpleNamespace(data=self.data)


class EmptySearch(BaseSearchProvider):
    async def search(self, query, max_results=5, *, language=None):
        return []


class FakeArchive:
    enabled = True

    def __init__(self, similar):
        self.similar = similar

    async def find_similar_by_url(self, url, limit=5):
        return self.similar[:limit]

    async def get_article_by_url(self, url):
        return None

    async def first_dead_check(self, article_id):
        return None

    async def audits_created_between(self, start, end):
        return []

    async def close(self):
        pass


@pytest.fixture
def run_offline_audit(monkeypatch):
    """Real graph and fetch parsing; all external boundaries are in-process fakes."""

    async def run(
        article, *, similar=(), pages=None, relevant=True, source_result=None,
        web_result=None,
    ):
        pages = pages or {}
        assert all(is_public_web_url(url) for url in [article.url, *pages])

        def handler(request):
            body = pages.get(str(request.url))
            return httpx.Response(
                200 if body is not None else 404,
                text=body or "",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        # Import-time capture_fetch wrappers still reach this transport boundary.
        monkeypatch.setattr(
            fetch_module,
            "_guarded_transport",
            lambda limits: httpx.MockTransport(handler),
        )
        engine = AuditEngine({"model_enabled": True})
        await engine.archive.close()
        engine.archive = FakeArchive(similar)
        engine.search = EmptySearch()
        engine.agents = SimpleNamespace(
            classification=SimpleNamespace(
                coarse=FakeAgent(classification.CoarseClassification()),
                fine=FakeAgent(classification.FineClassification()),
            ),
            relevance=FakeAgent(
                relevance.RelevanceResult(
                    is_relevant=relevant,
                    confidence=0.9,
                    reasoning="Invented boundary",
                )
            ),
            related_removals=FakeAgent("NONE"),
            source=FakeAgent(source_result or source.SourceResult()),
            web_search=FakeAgent(
                web_result or web_search.WebSearchResult(
                    reasoning="No invented match selected",
                )
            ),
        )
        try:
            result = await engine.run(AuditRequest(removed_article=article))
        finally:
            await engine.close()
        assert not any(step["status"] == "failed" for step in result["steps"])
        return result

    return run


def candidate():
    return SimilarArticle(id=71001, link=NEW_URL, title=budget_article().title)


def details(result, name):
    return next(
        s["output"]["details"] for s in result["steps"] if s["name"] == name
    )


def measure(record_property, name, old, new):
    values = directional_containments(old, new)
    assert all(value is not None for value in values)
    record_property(name, values)
    return values


async def test_verbatim_relocation_short_circuits(
    run_offline_audit, record_property
):
    result = await run_offline_audit(
        budget_article(),
        similar=[candidate()],
        pages={NEW_URL: page(BUDGET)},
    )
    measured = details(result, "similarity")["candidates"][0]
    record_property(
        "verbatim", (measured["old_in_new"], measured["new_in_old"])
    )
    assert measured["old_in_new"] >= 0.99
    assert result["response"]["verdict"] == "MOVED"
    assert result["response"]["short_circuited_at"] == "similarity"


async def low_interest_copy(run_offline_audit):
    result = await run_offline_audit(
        budget_article(),
        similar=[candidate()],
        pages={NEW_URL: shortened_budget()},
        relevant=False,
    )
    measured = details(result, "similarity")["candidates"][0]
    assert 0.85 <= measured["old_in_new"] <= 0.98
    return result, measured


async def test_low_interest_copy_below_floor_gets_relocation_note(
    run_offline_audit, record_property
):
    result = await run_offline_audit(
        budget_article(),
        similar=[candidate()],
        pages={NEW_URL: page(BUDGET[:1] + ORCHARD)},
        relevant=False,
    )
    measured = details(result, "similarity")["candidates"][0]
    record_property("below_floor", (measured["old_in_new"], measured["new_in_old"]))
    assert SIBLING_MAX < measured["old_in_new"] < pipeline.LOW_INTEREST_RELOCATION_FLOOR
    assert measured["fit"] == "partial"
    assert result["response"]["verdict"] == "LIKELY_LOW_PUBLIC_INTEREST"
    findings = result["response"]["findings"]
    assert "Relocation note:" in findings
    assert NEW_URL in findings
    assert f"holds {int(measured['old_in_new'] * 100)}% of" in findings
    assert f"{measured['new_in_old']:.0%}" in findings
    assert f"below the {pipeline.LOW_INTEREST_RELOCATION_FLOOR:.0%} floor" in findings


async def test_measured_copy_outlives_low_interest(run_offline_audit, record_property):
    result, measured = await low_interest_copy(run_offline_audit)
    record_property("partial", (measured["old_in_new"], measured["new_in_old"]))
    assert result["response"]["verdict"] == "LIKELY_MOVED"
    assert result["response"]["short_circuited_at"] == "relevance"
    assert result["response"]["flags"] == []
    findings = result["response"]["findings"]
    assert "substantial but partial" in findings
    assert "floor" not in findings
    assert "public-interest screen judged the article out of scope" in findings
    assert "Invented boundary" in findings
    assert "key-facts comparison of web search was not run" in findings
    assert "POSSIBLE_CENSORSHIP" not in findings
    assert [s["status"] for s in result["steps"][4:]] == ["skipped", "skipped"]


async def test_low_interest_sibling_keeps_original_response(
    run_offline_audit, record_property,
):
    result = await run_offline_audit(
        budget_article(), similar=[candidate()],
        pages={NEW_URL: page(ORCHARD)}, relevant=False,
    )
    measured = details(result, "similarity")["candidates"][0]
    record_property("sibling", (measured["old_in_new"], measured["new_in_old"]))
    assert measured["fit"] == "sibling"
    baseline = await run_offline_audit(budget_article(), relevant=False)
    assert result["response"]["verdict"] == "LIKELY_LOW_PUBLIC_INTEREST"
    assert result["response"]["findings"] == baseline["response"]["findings"]
    assert "Relocation note:" not in result["response"]["findings"]


def test_next_id_rewrite_is_below_sibling_bar(record_property):
    values = measure(
        record_property,
        "rewrite",
        html_to_text(page(BUDGET)),
        html_to_text(page(REWRITE)),
    )
    assert max(values) < SIBLING_MAX
    assert OLD_URL.split("/")[4] == "71000"
    assert NEW_URL.split("/")[4] == "71001"


async def next_id_rewrite(run_offline_audit):
    return await run_offline_audit(
        budget_article(),
        similar=[candidate()],
        pages={NEW_URL: page(REWRITE)},
        relevant=True,
        web_result=web_search.WebSearchResult(
            matches=[web_search.PageMatch(url=NEW_URL, covers_key_facts=True)],
            reasoning="Same invented water decision and dates, rewritten wording",
        ),
    )


async def test_next_id_rewrite_is_set_aside_today(run_offline_audit):
    result = await next_id_rewrite(run_offline_audit)
    measured = details(result, "similarity")["candidates"][0]
    assert max(measured["old_in_new"], measured["new_in_old"]) < SIBLING_MAX
    web = details(result, "web_search")
    assert web["related_same_publisher"] == [NEW_URL]
    assert web["relocated_to"] is None
    assert result["response"]["verdict"] == "INCONCLUSIVE"


@pytest.mark.xfail(strict=True, reason="stage B: same-publisher rewrite signal")
async def test_next_id_rewrite_is_recognised(run_offline_audit):
    result = await next_id_rewrite(run_offline_audit)
    assert result["response"]["verdict"] == "LIKELY_MOVED"
    assert details(result, "web_search")["relocated_to"] == NEW_URL


def footer_orchard():
    # The cited-source pair has a shared sentence; footer siblings do not.
    return ORCHARD[:-1] + [ORCHARD[-1].split(" Garden volunteers")[0]]


def footer_containments():
    removed = html_to_text(page(BUDGET + FOOTER))
    orchard = html_to_text(page(footer_orchard() + FOOTER))
    pottery = html_to_text(page(POTTERY + FOOTER))
    boilerplate = build_boilerplate(removed, [orchard, pottery])
    return measure_cleaned(
        removed, orchard, boilerplate,
    ).cleaned


def test_unrelated_articles_overlap_only_through_footer(record_property):
    orchard = footer_orchard()
    article_only = measure(
        record_property,
        "article_only",
        html_to_text(page(BUDGET)),
        html_to_text(page(orchard)),
    )
    full_page = measure(
        record_property,
        "footer",
        html_to_text(page(BUDGET + FOOTER)),
        html_to_text(page(orchard + FOOTER)),
    )
    assert max(article_only) < 0.01
    assert max(full_page) > 0.05


def test_shared_footer_is_dropped():
    assert max(footer_containments()) < 0.02


async def shared_footer_match(run_offline_audit, *, second_page=True):
    article = budget_article()
    article.content = page(BUDGET + FOOTER)
    similar = [SimilarArticle(
        id=71001, link=NEW_URL, title="Invented plateau orchard survey",
    )]
    pages = {NEW_URL: page(footer_orchard() + FOOTER)}
    if second_page:
        similar.append(SimilarArticle(
            id=71002, link=SECOND_URL, title="Invented pottery exhibition",
        ))
        pages[SECOND_URL] = page(POTTERY + FOOTER)
    return await run_offline_audit(
        article,
        similar=similar,
        pages=pages,
        relevant=True,
        web_result=web_search.WebSearchResult(
            matches=[web_search.PageMatch(url=NEW_URL, covers_key_facts=True)],
            reasoning="Invented model mistakes shared footer for the same story",
        ),
    )


async def test_shared_footer_is_set_aside_before_verdict(run_offline_audit):
    result = await shared_footer_match(run_offline_audit)
    candidates = details(result, "similarity")["candidates"]
    assert len(candidates) == 2
    for measured in candidates:
        assert measured["fit"] == "sibling"
        assert max(measured["old_in_new"], measured["new_in_old"]) < 0.02
        assert measured["boilerplate_paragraphs"] == [2, 2]
        assert max(measured["raw_old_in_new"], measured["raw_new_in_old"]) > SIBLING_MAX
    web = details(result, "web_search")
    assert web["related_same_publisher"] == [NEW_URL]
    assert web["relocated_to"] is None
    assert result["response"]["verdict"] == "INCONCLUSIVE"


async def test_lone_footer_page_still_counts_as_relocation(run_offline_audit):
    # With only one other live article there is no independent furniture vote.
    result = await shared_footer_match(run_offline_audit, second_page=False)
    measured = details(result, "similarity")["candidates"][0]
    assert measured["old_in_new"] == measured["raw_old_in_new"]
    assert measured["new_in_old"] == measured["raw_new_in_old"]
    assert measured["boilerplate_paragraphs"] == [0, 0]
    web = details(result, "web_search")
    assert max(web["old_in_new"], web["new_in_old"]) > SIBLING_MAX
    assert web["old_in_new"] == web["raw_old_in_new"]
    assert web["new_in_old"] == web["raw_new_in_old"]
    assert web["boilerplate_paragraphs"] == [0, 0]
    assert web["related_same_publisher"] == []
    assert web["relocated_to"] == NEW_URL
    assert result["response"]["verdict"] == "LIKELY_MOVED"


async def test_shared_footer_page_is_set_aside(run_offline_audit):
    result = await shared_footer_match(run_offline_audit)
    web = details(result, "web_search")
    assert web["relocated_to"] is None
    assert web["related_same_publisher"] == [NEW_URL]
    measured = web["same_publisher_measurements"][NEW_URL]
    assert max(measured["old_in_new"], measured["new_in_old"]) < 0.02
    assert max(measured["raw_old_in_new"], measured["raw_new_in_old"]) > SIBLING_MAX
    assert measured["boilerplate_paragraphs"] == [2, 2]
    assert result["response"]["verdict"] == "INCONCLUSIVE"


async def test_copy_with_changed_furniture_uses_cleaned_move_bar(run_offline_audit):
    article = budget_article()
    article.content = page(BUDGET + FOOTER)
    orchard_url = "https://dusulke.example/haber/71003/orchard-survey"
    result = await run_offline_audit(
        article,
        similar=[candidate(), SimilarArticle(id=71002, link=SECOND_URL),
                 SimilarArticle(id=71003, link=orchard_url)],
        pages={
            NEW_URL: page(BUDGET),
            SECOND_URL: page(POTTERY + FOOTER),
            orchard_url: page(footer_orchard() + FOOTER),
        },
    )
    measured = details(result, "similarity")["candidates"][0]
    assert measured["raw_old_in_new"] < MOVED_THRESHOLD
    assert measured["old_in_new"] >= MOVED_THRESHOLD
    assert measured["fit"] == "verbatim"
    assert measured["boilerplate_paragraphs"] == [2, 0]
    assert result["response"]["verdict"] == "MOVED"
    assert result["response"]["short_circuited_at"] == "similarity"


def live_page(url, paragraphs):
    return LivePage(
        url=url, final_url=url, status=200, title="Invented live page",
        text=html_to_text(page(paragraphs)),
    )


@pytest.mark.parametrize("excluded_vote_url", [OLD_URL, CITED_URL])
def test_web_boilerplate_ignores_own_url_and_other_publishers(excluded_vote_url):
    article = budget_article()
    article.content = page(BUDGET + FOOTER)
    fetched = FetchOutcome(live=[
        live_page(NEW_URL, footer_orchard() + FOOTER),
        live_page(excluded_vote_url, POTTERY + FOOTER),
    ])
    evidence = web_search._ground(web_search.WebSearchResult(
        matches=[web_search.PageMatch(url=NEW_URL)], reasoning="Invented match",
    ), fetched, article, 2)
    assert evidence.relocated_to.url == NEW_URL
    assert evidence.relocation_old_in_new == evidence.raw_old_in_new
    assert evidence.relocation_new_in_old == evidence.raw_new_in_old
    assert evidence.boilerplate_paragraphs == (0, 0)


def test_web_relocation_records_cleaned_and_raw_copy_scores():
    article = budget_article()
    article.content = page(BUDGET + FOOTER)
    fetched = FetchOutcome(live=[
        live_page(NEW_URL, BUDGET),
        live_page(SECOND_URL, footer_orchard() + FOOTER),
        live_page("https://dusulke.example/haber/71003/pottery", POTTERY + FOOTER),
    ])
    evidence = web_search._ground(web_search.WebSearchResult(
        matches=[web_search.PageMatch(url=NEW_URL)], reasoning="Invented copy",
    ), fetched, article, 3)
    assert evidence.relocation_old_in_new >= MOVED_THRESHOLD
    assert evidence.raw_old_in_new < MOVED_THRESHOLD
    assert evidence.boilerplate_paragraphs == (2, 0)
    assert evidence.same_publisher_measurements[NEW_URL].old_in_new >= MOVED_THRESHOLD
    assert pipeline._decide(evidence)[0].value == "MOVED"
    recorded = web_search.overlap_details(evidence)
    assert recorded["boilerplate_paragraphs"] == [2, 0]
    assert recorded["same_publisher_measurements"][NEW_URL]["raw_old_in_new"] == (
        evidence.raw_old_in_new
    )


async def test_web_boilerplate_combines_prefetched_pages_and_new_hits(monkeypatch):
    article = budget_article()
    article.content = page(BUDGET + FOOTER)
    prefetched = FetchOutcome(live=[live_page(NEW_URL, footer_orchard() + FOOTER)])

    class SecondPageSearch(BaseSearchProvider):
        async def search(self, query, max_results=5, *, language=None):
            return [SearchResult(
                url=SECOND_URL, title="Invented pottery exhibit", snippet="",
            )]

    async def fetch(hits, *, exclude_url):
        assert [hit.url for hit in hits] == [SECOND_URL]
        assert exclude_url == OLD_URL
        return FetchOutcome(live=[live_page(SECOND_URL, POTTERY + FOOTER)])

    monkeypatch.setattr(web_search, "fetch_candidates", fetch)
    evidence = await web_search.run(
        article, SecondPageSearch(), FakeAgent(web_search.WebSearchResult(
            matches=[web_search.PageMatch(url=NEW_URL)], reasoning="Invented match",
        )), prefetched=prefetched,
    )
    assert evidence.live_pages_read == 2
    assert evidence.relocated_to is None
    assert [hit.url for hit in evidence.related_same_publisher] == [NEW_URL]
    measured = evidence.same_publisher_measurements[NEW_URL]
    assert measured.boilerplate_paragraphs == (2, 2)
    assert max(measured.cleaned) < 0.02
    assert max(measured.raw) > SIBLING_MAX


async def cited_different_story(run_offline_audit):
    article = budget_article()
    attribution = "Bulut Defteri haberine göre su bütçesi görüşüldü."
    article.content += (
        "<p>" + attribution + f' <a href="{CITED_URL}">Kaynak metni</a></p>'
    )
    result = await run_offline_audit(
        article,
        pages={CITED_URL: page(ORCHARD)},
        source_result=source.SourceResult(
            source_kind="other_outlet",
            source_name="Bulut Defteri",
            source_url=CITED_URL,
            attribution_quote=attribution,
        ),
    )
    return details(result, "source")


async def test_cited_page_with_other_story_is_not_a_live_copy(
    run_offline_audit,
    record_property,
):
    measured = await cited_different_story(run_offline_audit)
    record_property("cited", measured["source_containment"])
    assert 0.01 < measured["source_containment"] < 0.1
    assert measured["source_status"] == "live_other"
