"""Invented page families covering relocation, boilerplate and dated rewrites."""

from datetime import datetime
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
from audit.search.fetch import FetchOutcome, LivePage, html_to_text, normalize_url
from audit.search.rewrite import distinctive_words, word_overlap
from audit.search.textmatch import MOVED_THRESHOLD, PARTIAL_MIN, SIBLING_MAX, directional_containments
from audit.search.urls import is_public_web_url
from audit.workflow import classification, pipeline, relevance, similarity, source, web_search


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

# INVENTED: the same fictional water decision, retaining facts and vocabulary
# while changing sentence structure rather than copying five-word phrases.
REWRITE_FACTS = [
    "The fictional Karaçam council set its water budget on 12 March 2031. "
    "Three tanks for storage and a pump replacement at the northern reservoir "
    "received approval. Before voting, members checked household demand, "
    "expected costs for construction and the delivery schedule. Repairs, the "
    "mayor explained, would start ahead of the dry season. During these works, "
    "temporary pipes would keep streets in the surrounding area supplied.",
    "Separate equipment inspections and purchases of electricity will receive "
    "funds from the finance committee. Households on assistance keep their "
    "discount; the quarterly index for inflation determines water tariffs. "
    "Monthly statements of spending were requested by an opposition member, "
    "allowing residents to track every contract. Pressure readings will be "
    "published by engineers, alongside explanations of the additional capacity "
    "for pumping needed in hillside streets.",
    "Questions from residents focused on collection at communal taps and "
    "whether times might change. Written notices will precede planned "
    "interruptions, according to the department, whose telephone service will "
    "answer questions in the evening. After recording the vote, clerks put "
    "tables approved by council in its public room for reading. A June meeting "
    "will review progress with delivery and collect observations from "
    "neighbourhood committees. For seedlings, garden volunteers requested "
    "an illustrated leaflet prepared separately.",
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
        web_result=None, web_agent=None,
    ):
        pages = pages or {}
        assert all(is_public_web_url(url) for url in [article.url, *pages])

        def handler(request):
            body = pages.get(str(request.url))
            if isinstance(body, httpx.Response):
                return body
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
            web_search=web_agent or FakeAgent(
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


async def test_pure_paraphrase_stays_set_aside_by_design(run_offline_audit):
    # Its vocabulary overlap is even below the unrelated invented orchard page.
    own = distinctive_words(html_to_text(page(BUDGET)))
    rewrite_share, _ = word_overlap(own, html_to_text(page(REWRITE)))
    unrelated_share, _ = word_overlap(own, html_to_text(page(ORCHARD)))
    assert rewrite_share < unrelated_share < 0.35
    result = await run_offline_audit(
        dated_budget(), similar=[dated_rewrite_candidate()],
        pages={NEW_URL: page(REWRITE)}, web_result=rewrite_match(),
    )
    measured = details(result, "similarity")["candidates"][0]
    assert measured["date_gap_days"] == 2
    assert measured["title_overlap"] >= 0.35
    assert measured["rewrite"] is False
    assert result["response"]["verdict"] == "INCONCLUSIVE"
    assert details(result, "web_search")["relocated_to"] is None



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


async def test_copy_without_the_site_furniture_goes_on_to_web_search(run_offline_audit):
    # The other live pages share the footer; the copy is on a template without it. The
    # footer stays in the removed article's score, so the copy is a partial fit and the
    # audit goes on to web search instead of stopping at a measured move.
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
    assert PARTIAL_MIN <= measured["raw_old_in_new"] < MOVED_THRESHOLD
    assert measured["old_in_new"] == measured["raw_old_in_new"]
    assert measured["fit"] == "partial"
    assert measured["boilerplate_paragraphs"] == [0, 0]
    assert result["response"]["verdict"] != "MOVED"
    assert result["response"]["short_circuited_at"] != "similarity"
    assert any(step["name"] == "web_search" for step in result["steps"])


ARCHIVED_FOOTER = ("Explore our publication archives and contact the reader desk for general "
                   "subscription assistance delivery questions and account settings")


@pytest.mark.parametrize("archived,live", [
    (FOOTER, FOOTER),
    ([ARCHIVED_FOOTER], [ARCHIVED_FOOTER + " online"]),
], ids=["exact copy with learned footer", "revised site footer"])
async def test_copy_with_the_site_furniture_still_moves(run_offline_audit, archived, live):
    # Two other live pages share the site's current footer, so it is learned. A copy that
    # carries it (unchanged, or revised by one word since archiving) is still a measured move.
    article = budget_article()
    article.content = page(BUDGET + archived)
    orchard_url = "https://dusulke.example/haber/71003/orchard-survey"
    result = await run_offline_audit(
        article,
        similar=[candidate(), SimilarArticle(id=71002, link=SECOND_URL),
                 SimilarArticle(id=71003, link=orchard_url)],
        pages={
            NEW_URL: page(BUDGET + live),
            SECOND_URL: page(POTTERY + live),
            orchard_url: page(footer_orchard() + live),
        },
    )
    measured = details(result, "similarity")["candidates"][0]
    assert measured["old_in_new"] >= MOVED_THRESHOLD
    assert measured["fit"] == "verbatim"
    assert measured["boilerplate_paragraphs"][1] == len(live)
    assert result["response"]["verdict"] == "MOVED"
    assert result["response"]["short_circuited_at"] == "similarity"


async def test_copy_missing_a_repeated_background_paragraph_is_not_moved(run_offline_audit):
    # Two follow-up stories on the site repeat the original's middle paragraph word for
    # word, so it is learned as same-site boilerplate. The relocated page is the original
    # without that paragraph: a partial copy, never a measured MOVED.
    background = BUDGET[1]
    orchard_url = "https://dusulke.example/haber/71003/orchard-survey"
    result = await run_offline_audit(
        budget_article(),
        similar=[candidate(), SimilarArticle(id=71002, link=SECOND_URL),
                 SimilarArticle(id=71003, link=orchard_url)],
        pages={
            NEW_URL: page([BUDGET[0], BUDGET[2]]),
            SECOND_URL: page(POTTERY + [background]),
            orchard_url: page(footer_orchard() + [background]),
        },
    )
    measured = details(result, "similarity")["candidates"][0]
    assert PARTIAL_MIN <= measured["raw_old_in_new"] < MOVED_THRESHOLD
    assert measured["old_in_new"] == measured["raw_old_in_new"]
    assert measured["boilerplate_paragraphs"][0] == 0
    assert measured["fit"] == "partial"
    assert result["response"]["verdict"] != "MOVED"
    assert result["response"]["short_circuited_at"] != "similarity"
    assert any(step["name"] == "web_search" for step in result["steps"])


def live_page(url, paragraphs):
    return LivePage(
        url=url, final_url=url, status=200, title="Invented live page",
        text=html_to_text(page(paragraphs)),
    )


def redirected_quote_pages():
    pages = [
        live_page(SECOND_URL, ORCHARD + BUDGET[:1]),
        live_page("https://dusulke.example/quoted-reading", POTTERY + BUDGET[:1]),
    ]
    for index, fetched in enumerate(pages):
        fetched.final_url = f"https://other.example/quoted-reading-{index}"
    return pages


@pytest.mark.parametrize("include_redirected_matches", [False, True])
def test_redirected_quotes_cannot_strip_local_partial_copy(include_redirected_matches):
    partial = live_page(NEW_URL, BUDGET[:1] + [" ".join(
        f"rewrittenlocal{n}" for n in range(90)
    )])
    redirected = redirected_quote_pages()
    # The lede supplies > 30% raw containment; stripping it leaves no overlap.
    assert directional_containments(html_to_text(page(BUDGET)), partial.text)[0] > 0.30
    matches = ([web_search.PageMatch(url=p.url) for p in redirected]
               if include_redirected_matches else [])
    matches.append(web_search.PageMatch(url=NEW_URL))
    evidence = web_search._ground(web_search.WebSearchResult(
        matches=matches, reasoning="Invented partial copy and external quotes",
    ), FetchOutcome(live=[partial, *redirected]), budget_article(), 3)
    assert evidence.relocated_to is not None
    assert evidence.relocated_to.url == NEW_URL
    assert evidence.relocation_old_in_new == evidence.raw_old_in_new
    assert evidence.boilerplate_paragraphs == (0, 0)
    assert set(evidence.same_publisher_measurements) == {NEW_URL}
    assert pipeline._decide(evidence)[0].value == "LIKELY_MOVED"


async def test_similarity_redirected_quotes_cannot_vote_or_be_measured(monkeypatch):
    partial = live_page(NEW_URL, BUDGET[:1] + [" ".join(
        f"rewrittenlocal{n}" for n in range(90)
    )])
    pages = [partial, *redirected_quote_pages()]

    async def fetched(*args, **kwargs):
        return FetchOutcome(live=pages)

    monkeypatch.setattr(similarity, "fetch_candidates", fetched)
    evidence = await similarity.run(budget_article(), client=FakeArchive([
        SimilarArticle(id=73000 + n, link=p.url) for n, p in enumerate(pages)
    ]))
    local, *redirected = evidence.candidates
    assert local.old_in_new == local.raw_old_in_new
    assert local.old_in_new > 0.30
    assert local.boilerplate_paragraphs == (0, 0)
    for cand in redirected:
        assert not cand.same_publisher
        assert cand.old_in_new is None
        assert cand.boilerplate_paragraphs is None


async def test_majority_boilerplate_cap_reaches_similarity_and_web_details(run_offline_audit):
    article = budget_article()
    article.content = page(BUDGET[:1] + FOOTER)
    # The two footer paragraphs exceed half this shortened original. The
    # orchard/pottery pages may learn them, but neither scoring side is stripped.
    result = await run_offline_audit(
        article,
        similar=[candidate(), SimilarArticle(id=71002, link=SECOND_URL)],
        pages={
            NEW_URL: page(footer_orchard() + FOOTER),
            SECOND_URL: page(POTTERY + FOOTER),
        },
        web_result=web_search.WebSearchResult(
            matches=[web_search.PageMatch(url=NEW_URL)],
            reasoning="Invented majority furniture cap",
        ),
    )
    for measured in details(result, "similarity")["candidates"]:
        assert measured["boilerplate_capped"] is True
        assert measured["boilerplate_paragraphs"] == [2, 2]
        assert measured["old_in_new"] == measured["raw_old_in_new"]
        assert measured["new_in_old"] == measured["raw_new_in_old"]
    web = details(result, "web_search")
    assert web["boilerplate_capped"] is True
    assert web["boilerplate_paragraphs"] == [2, 2]
    assert web["old_in_new"] == web["raw_old_in_new"]
    assert web["new_in_old"] == web["raw_new_in_old"]
    assert web["same_publisher_measurements"][NEW_URL]["boilerplate_capped"] is True


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


def test_web_relocation_without_the_site_furniture_stays_below_the_move_bar():
    # Web search applies the same rule: the footer the copy lacks stays in the score.
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
    assert PARTIAL_MIN <= evidence.raw_old_in_new < MOVED_THRESHOLD
    assert evidence.relocation_old_in_new == evidence.raw_old_in_new
    assert evidence.boilerplate_paragraphs == (0, 0)
    assert evidence.same_publisher_measurements[NEW_URL].old_in_new < MOVED_THRESHOLD
    assert pipeline._decide(evidence)[0].value != "MOVED"
    recorded = web_search.overlap_details(evidence)
    assert recorded["boilerplate_paragraphs"] == [0, 0]
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


def dated_rewrite_candidate(**changes):
    return SimilarArticle(**(dict(
        id=71001, link=NEW_URL,
        title="Karaçam council water budget and reservoir plans for March 2031",
        publish_date="2031-03-14T09:00:00Z",
    ) | changes))


def dated_budget():
    return budget_article().model_copy(update={"published_at": datetime(2031, 3, 12)})


def rewrite_match():
    return web_search.WebSearchResult(
        matches=[web_search.PageMatch(url=NEW_URL)], reasoning="Same invented budget facts",
    )


def test_factual_rewrite_changes_phrases_but_keeps_distinctive_words(record_property):
    values = measure(record_property, "factual_rewrite", html_to_text(page(BUDGET)),
                     html_to_text(page(REWRITE_FACTS)))
    assert max(values) < SIBLING_MAX
    share, count = word_overlap(distinctive_words(html_to_text(page(BUDGET))),
                                html_to_text(page(REWRITE_FACTS)))
    assert share >= 0.35
    assert count >= 20


async def test_factual_rewrite_model_match_is_likely_moved(run_offline_audit):
    result = await run_offline_audit(
        dated_budget(), similar=[dated_rewrite_candidate()],
        pages={NEW_URL: page(REWRITE_FACTS)}, web_result=rewrite_match(),
    )
    measured = details(result, "similarity")["candidates"][0]
    assert measured["rewrite"] is True
    assert measured["title_overlap"] >= 0.35
    assert max(measured["old_in_new"], measured["new_in_old"]) < SIBLING_MAX
    web = details(result, "web_search")
    assert web["relocated_to"] == NEW_URL
    assert web["relocation_rewrite"]["word_overlap"] == measured["word_overlap"]
    assert web["related_same_publisher"] == []
    response = result["response"]
    assert response["verdict"] == "LIKELY_MOVED"
    assert response["flags"] == []
    findings = response["findings"]
    assert "Same-publisher rewrite" in findings
    assert f"{int(measured['title_overlap'] * 100)}% of the original's headline words" in findings
    assert f"{int(measured['word_overlap'] * 100)}% of the original's distinctive words" in findings
    assert "published 2 days apart" in findings
    assert "wording differs" in findings
    assert "human auditor should compare" in findings


async def test_factual_rewrite_requires_model_same_story_match(run_offline_audit):
    result = await run_offline_audit(
        dated_budget(), similar=[dated_rewrite_candidate()], pages={NEW_URL: page(REWRITE_FACTS)},
    )
    assert details(result, "similarity")["rewrite"] is not None
    assert details(result, "web_search")["relocated_to"] is None
    assert details(result, "web_search")["relocation_rewrite"] is None
    assert result["response"]["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("rewrite_first", [True, False])
async def test_rewrite_never_preempts_partial_copy_missing_key_facts(
    run_offline_audit, rewrite_first,
):
    matches = [
        web_search.PageMatch(url=NEW_URL),
        web_search.PageMatch(url=SECOND_URL, covers_key_facts=False,
                             omissions=["Monthly spending statements"]),
    ]
    if not rewrite_first:
        matches.reverse()
    candidates = [dated_rewrite_candidate(), SimilarArticle(
        id=71002, link=SECOND_URL, title=dated_budget().title,
    )]
    kwargs = dict(
        pages={NEW_URL: page(REWRITE_FACTS), SECOND_URL: page(BUDGET[:1] + ORCHARD)},
        web_result=web_search.WebSearchResult(matches=matches, reasoning="Invented copies"),
    )
    result = await run_offline_audit(dated_budget(), similar=candidates, **kwargs)
    measured = details(result, "similarity")["candidates"]
    assert measured[0]["rewrite"] is True
    assert max(measured[0]["old_in_new"], measured[0]["new_in_old"]) < SIBLING_MAX
    assert measured[1]["fit"] == "partial"
    assert measured[1]["old_in_new"] >= SIBLING_MAX
    web = details(result, "web_search")
    assert web["relocated_to"] == SECOND_URL
    assert web["covers_key_facts"] is False
    assert web["omissions"] == ["Monthly spending statements"]
    assert web["relocation_rewrite"] is None
    assert result["response"]["verdict"] == "POSSIBLE_CENSORSHIP"
    assert f"to {SECOND_URL}" in result["response"]["findings"]

    # The same pages and model reports without a dated rewrite signal must agree.
    candidates[0] = dated_rewrite_candidate(publish_date=None)
    baseline = await run_offline_audit(dated_budget(), similar=candidates, **kwargs)
    assert details(baseline, "similarity")["rewrite"] is None
    assert details(baseline, "web_search")["relocated_to"] == SECOND_URL
    for field in ("verdict", "findings", "flags"):
        assert result["response"][field] == baseline["response"][field]


async def test_prompt_keeps_third_partial_copy_and_appends_fourth_rewrite(run_offline_audit):
    unrelated = [f"https://dusulke.example/unrelated/{i}" for i in range(2)]
    urls = [*unrelated, SECOND_URL, NEW_URL]
    candidates = [
        SimilarArticle(id=72000 + i, link=url, title="Invented unrelated headline")
        for i, url in enumerate(unrelated)
    ] + [
        SimilarArticle(id=71002, link=SECOND_URL, title=dated_budget().title),
        dated_rewrite_candidate(),
    ]
    prompts = []

    async def judge(prompt, **kwargs):
        prompts.append(prompt)
        assert all(url in prompt for url in urls)
        assert [line.split(" (")[1].removesuffix(")") for line in prompt.splitlines()
                if line.startswith("- ")] == urls
        return SimpleNamespace(data=web_search.WebSearchResult(
            matches=[match for match in [
                web_search.PageMatch(url=NEW_URL),
                web_search.PageMatch(url=SECOND_URL, covers_key_facts=False,
                                     omissions=["Monthly spending statements"]),
            ] if match.url in prompt], reasoning="Invented pages shown in the prompt",
        ))

    result = await run_offline_audit(
        dated_budget(), similar=candidates, web_agent=SimpleNamespace(run=judge),
        pages={unrelated[0]: page(ORCHARD), unrelated[1]: page(POTTERY),
               SECOND_URL: page(BUDGET[:1] + ORCHARD), NEW_URL: page(REWRITE_FACTS)},
    )
    assert len(prompts) == 1
    assert details(result, "similarity")["rewrite"]["url"] == NEW_URL
    web = details(result, "web_search")
    assert web["relocated_to"] == SECOND_URL
    assert web["covers_key_facts"] is False
    assert web["omissions"] == ["Monthly spending statements"]
    assert result["response"]["verdict"] == "POSSIBLE_CENSORSHIP"
    assert f"to {SECOND_URL}" in result["response"]["findings"]


async def test_engine_hands_only_best_rewrite_to_web_search(run_offline_audit, monkeypatch):
    original_run = web_search.run
    forwarded = []

    async def inspect_rewrites(*args, **kwargs):
        forwarded.append(kwargs["rewrites"])
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(web_search, "run", inspect_rewrites)
    result = await run_offline_audit(
        dated_budget(), similar=[
            dated_rewrite_candidate(),
            dated_rewrite_candidate(id=71002, link=SECOND_URL),
        ], pages={NEW_URL: page(REWRITE_FACTS), SECOND_URL: page(REWRITE_FACTS)},
        web_result=web_search.WebSearchResult(matches=[
            web_search.PageMatch(url=SECOND_URL), web_search.PageMatch(url=NEW_URL),
        ], reasoning="Invented rewrites"),
    )
    sim = details(result, "similarity")
    assert all(c["rewrite"] for c in sim["candidates"])
    assert sim["rewrite"]["url"] == NEW_URL
    assert len(forwarded) == 1
    assert list(forwarded[0]) == [normalize_url(NEW_URL)]
    assert details(result, "web_search")["relocated_to"] == NEW_URL


async def test_template_headline_with_unrelated_words_stays_set_aside(run_offline_audit):
    result = await run_offline_audit(
        dated_budget(), similar=[dated_rewrite_candidate()],
        pages={NEW_URL: page(ORCHARD)}, web_result=rewrite_match(),
    )
    measured = details(result, "similarity")["candidates"][0]
    assert measured["title_overlap"] >= 0.35
    assert measured["word_overlap"] < 0.30
    assert measured["rewrite"] is False
    assert details(result, "web_search")["related_same_publisher"] == [NEW_URL]
    assert details(result, "web_search")["relocated_to"] is None
    baseline = await run_offline_audit(dated_budget())
    assert result["response"]["verdict"] == baseline["response"]["verdict"]
    assert result["response"]["flags"] == []


async def test_low_interest_factual_rewrite_decides_without_web_search(run_offline_audit):
    result = await run_offline_audit(
        dated_budget(), similar=[dated_rewrite_candidate()],
        pages={NEW_URL: page(REWRITE_FACTS)}, relevant=False,
    )
    response = result["response"]
    assert response["verdict"] == "LIKELY_MOVED"
    assert response["short_circuited_at"] == "relevance"
    assert response["flags"] == []
    assert "same-publisher rewrite from the similarity step decides" in response["findings"]
    assert "key-facts comparison of web search was not run" in response["findings"]
    assert [s["status"] for s in result["steps"][4:]] == ["skipped", "skipped"]


@pytest.mark.parametrize("same_headline", [True, False])
@pytest.mark.parametrize("relevant", [True, False])
async def test_stub_needs_near_identical_headline(run_offline_audit, same_headline, relevant):
    article = dated_budget().model_copy(update={"content": "Brief update"})
    candidate = dated_rewrite_candidate(
        title=article.title if same_headline else "Invented pottery exhibition opens",
        publish_date="2031-03-13",
    )
    result = await run_offline_audit(
        article, similar=[candidate], pages={NEW_URL: page([article.title] + REWRITE_FACTS)},
        relevant=relevant, web_result=rewrite_match(),
    )
    response = result["response"]
    if same_headline:
        assert response["verdict"] == "LIKELY_MOVED"
        assert "original's text was too short to compare" in response["findings"]
        assert "published 1 day apart" in response["findings"]
        assert "wording differs" not in response["findings"]
        assert "0%" not in response["findings"].replace("100%", "")
    else:
        assert response["verdict"] == ("INCONCLUSIVE" if relevant else "LIKELY_LOW_PUBLIC_INTEREST")
    if relevant:
        assert (details(result, "web_search")["relocated_to"] == NEW_URL) is same_headline


@pytest.mark.parametrize("redirect", [True, False])
async def test_stub_with_matching_archive_headline_needs_live_page_evidence(
    run_offline_audit, redirect,
):
    article = dated_budget().model_copy(update={"content": "Brief update"})
    candidate = dated_rewrite_candidate(title=article.title, publish_date="2031-03-13")
    pages = {NEW_URL: page(POTTERY)}
    if redirect:
        home = "https://dusulke.example/"
        pages = {
            NEW_URL: httpx.Response(302, headers={"location": home}),
            home: page(POTTERY),
        }
    result = await run_offline_audit(article, similar=[candidate], pages=pages, relevant=False)
    measured = details(result, "similarity")["candidates"][0]
    assert measured["status"] == "live"
    assert measured["title_overlap"] == 1
    assert measured["headline_in_text"] == 0
    assert measured["rewrite"] is False
    assert result["response"]["verdict"] == "LIKELY_LOW_PUBLIC_INTEREST"


@pytest.mark.parametrize(("destination", "expected_rewrite", "verdict"), [
    ("https://dusulke.example/", False, "LIKELY_LOW_PUBLIC_INTEREST"),
    ("https://dusulke.example/section", False, "LIKELY_LOW_PUBLIC_INTEREST"),
    ("https://dusulke.example/haber/71001/changed-slug", True, "LIKELY_MOVED"),
])
async def test_long_rewrite_redirect_must_stay_on_same_article(
    run_offline_audit, destination, expected_rewrite, verdict,
):
    result = await run_offline_audit(
        dated_budget(), similar=[dated_rewrite_candidate()], relevant=False,
        pages={NEW_URL: httpx.Response(302, headers={"location": destination}),
               destination: page(REWRITE_FACTS)},
    )
    sim = details(result, "similarity")
    measured = sim["candidates"][0]
    assert measured["status"] == "live"
    assert measured["final_url"] == destination
    assert measured["own_words"] >= 20
    assert measured["title_overlap"] >= 0.35
    assert measured["word_overlap"] >= 0.35
    assert measured["date_gap_days"] == 2
    assert max(measured["old_in_new"], measured["new_in_old"]) < SIBLING_MAX
    assert measured["rewrite"] is expected_rewrite
    assert (sim["rewrite"] is not None) is expected_rewrite
    assert result["response"]["verdict"] == verdict


async def test_factual_rewrite_without_any_archive_date_fails_closed(run_offline_audit):
    result = await run_offline_audit(
        dated_budget(), similar=[dated_rewrite_candidate(publish_date=None)],
        pages={NEW_URL: page(REWRITE_FACTS)}, web_result=rewrite_match(),
    )
    measured = details(result, "similarity")["candidates"][0]
    assert measured["title_overlap"] >= 0.35
    assert measured["word_overlap"] >= 0.35
    assert measured["date_gap_days"] is None
    assert measured["rewrite"] is False
    assert details(result, "web_search")["relocated_to"] is None
    assert result["response"]["verdict"] == "INCONCLUSIVE"
