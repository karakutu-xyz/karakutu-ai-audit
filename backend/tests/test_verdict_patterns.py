"""Invented page pairs recording current gaps and future stage A/B targets."""

from html import escape
from types import SimpleNamespace

import httpx
import pytest

from audit.clients.models import SimilarArticle
from audit.domain import Article, AuditRequest
from audit.engine import AuditEngine
from audit.search import fetch as fetch_module
from audit.search.base import BaseSearchProvider
from audit.search.fetch import html_to_text
from audit.search.textmatch import SIBLING_MAX, directional_containments
from audit.search.urls import is_public_web_url
from audit.workflow import classification, relevance, source, web_search


OLD_URL = "https://dusulke.example/haber/71000/water-budget"
NEW_URL = "https://dusulke.example/haber/71001/water-budget-revised"
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
        article, *, similar=(), pages=None, relevant=True, source_result=None
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
                web_search.WebSearchResult(
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


async def test_low_interest_hides_copy_today(
    run_offline_audit, record_property
):
    result, measured = await low_interest_copy(run_offline_audit)
    record_property("partial", (measured["old_in_new"], measured["new_in_old"]))
    assert result["response"]["verdict"] == "LIKELY_LOW_PUBLIC_INTEREST"


@pytest.mark.xfail(
    strict=True,
    reason="stage A: a measured same-publisher copy outlives the low-interest exit",
)
async def test_measured_copy_outlives_low_interest(run_offline_audit):
    result, _ = await low_interest_copy(run_offline_audit)
    assert result["response"]["verdict"] == "LIKELY_MOVED"


def _recognised_as_rewrite(old_url, old_text, new_url, new_text):
    """Stage B replaces this placeholder with the real rewrite signal."""
    return False


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


@pytest.mark.xfail(strict=True, reason="stage B: same-publisher rewrite signal")
def test_next_id_rewrite_is_recognised():
    assert _recognised_as_rewrite(
        OLD_URL,
        html_to_text(page(BUDGET)),
        NEW_URL,
        html_to_text(page(REWRITE)),
    )


def footer_orchard():
    # The cited-source pair has a shared sentence; footer siblings do not.
    return ORCHARD[:-1] + [ORCHARD[-1].split(" Garden volunteers")[0]]


def footer_containments():
    orchard = footer_orchard()
    return directional_containments(
        html_to_text(page(BUDGET + FOOTER)),
        html_to_text(page(orchard + FOOTER)),
    )


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


@pytest.mark.xfail(
    strict=True,
    reason="stage B: shared boilerplate is dropped before scoring",
)
def test_shared_footer_is_dropped():
    assert max(footer_containments()) < 0.02


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


async def test_cited_page_is_live_despite_different_story(
    run_offline_audit,
    record_property,
):
    measured = await cited_different_story(run_offline_audit)
    record_property("cited", measured["source_containment"])
    assert measured["source_status"] == "live"
    assert 0.01 < measured["source_containment"] < 0.1


@pytest.mark.xfail(
    strict=True,
    reason="stage A: a cited page is the same story only above a measured overlap",
)
async def test_cited_page_is_not_reported_as_live_copy(run_offline_audit):
    measured = await cited_different_story(run_offline_audit)
    # Stage A should set source_status to a not-same-story status here.
    assert measured["source_status"] != "live"
