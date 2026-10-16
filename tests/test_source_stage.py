"""Step 4 — find the source of the article and check whether it is live.

`probe_removed_url` and `fetch_candidates` are stubbed (no sockets); TestModel plays the model
with exact outputs so the grounding rules are what is under test."""
import pytest
from pydantic_ai.models.test import TestModel

from src.api.models import Article
from src.search.base import SearchResult
from src.search.fetch import DeadCandidate, FetchOutcome, LivePage, UnreadableCandidate
from src.workflow import source
from src.workflow.source import (
    LIVE_SELF_MIN_CONTAINMENT, SourceResult, build_agent, build_prompt, extract_attribution_phrases,
    extract_links, ground_claim, host_for,
)

BODY = " ".join(f"kelime{i} sözcük{i} olay{i}" for i in range(40))
HTML = (
    "<p>Örnek Gazete'nin haberine göre; bu ihale belgeleri kamuya açıklanmadı.</p>"
    f"<p>{BODY}</p>"
    '<p>Ayrıntılar için <a href="https://www.gazete.example/ihale-belgeleri-p123">Örnek Gazete</a> ve '
    '<a href="https://www.daily.example.net/gundem/onceki-haber-1">önceki haberimiz</a>. '
    '<a href="https://twitter.com/share?url=x">Paylaş</a> '
    '<a href="https://example.org/rapor.pdf">rapor</a></p>'
    "<p>Kaynak: Anadolu Ajansı — bu haber ajans servisinden alınarak yayımlanmıştır.</p>"
)
REMOVED = Article(url="https://www.daily.example.net/gundem/ihale-haberi-2", title="Belediye ihalesi", content=HTML)


def live(url, text=BODY):
    return LivePage(url=url, final_url=url, status=200, title="t", text=text)


class FakeSearch:
    def __init__(self, hits=None):
        self.queries, self._hits = [], hits or []

    async def search(self, query, max_results=5, *, language=None):
        self.queries.append((query, max_results, language))
        return self._hits


@pytest.fixture
def net(monkeypatch):
    state = {"probe": None, "fetch": FetchOutcome(), "fetch_hits": None, "exclude_url": None}

    async def fake_probe(url, title=""):
        return state["probe"]

    async def fake_fetch(hits, exclude_url):
        state["fetch_hits"], state["exclude_url"] = hits, exclude_url
        return state["fetch"]

    monkeypatch.setattr(source, "probe_removed_url", fake_probe)
    monkeypatch.setattr(source, "fetch_candidates", fake_fetch)
    return state


def agent(**claim):
    return build_agent(TestModel(custom_result_args=SourceResult(**claim).model_dump()))


# --- extraction (pure) -------------------------------------------------------

def test_extract_links_keeps_off_host_article_links_only():
    assert extract_links(HTML, REMOVED.url) == ["https://www.gazete.example/ihale-belgeleri-p123"]


def test_extract_links_reads_bare_urls_too():
    assert extract_links("Detay: https://post.example.org/haber/x,123. Son.", REMOVED.url) == ["https://post.example.org/haber/x,123"]


def test_extract_attribution_phrases_finds_turkish_credits():
    phrases = extract_attribution_phrases(source.prose_of(HTML))
    assert any("haberine göre" in p for p in phrases)
    assert any(p.startswith("Kaynak:") for p in phrases)


def test_extract_attribution_phrases_finds_agency_datelines():
    phrases = extract_attribution_phrases("ANKARA (AA) - Bakanlık açıklama yaptı. Devamı.")
    assert phrases and "(AA)" in phrases[0]


def test_host_for_known_names_and_unknown_ones():
    assert host_for("Anadolu Ajansı") == "aa.com.tr"
    assert host_for("İHA") == "iha.com.tr"
    assert host_for("Sözcü'nün") == "sozcu.com.tr"
    assert host_for("Bilinmeyen Gazete") is None


# --- grounding (pure) --------------------------------------------------------

def test_ground_claim_keeps_a_name_and_url_that_are_really_in_the_text():
    prose = source.prose_of(HTML)
    links = extract_links(HTML, REMOVED.url)
    kind, name, url, quote, dropped = ground_claim(
        SourceResult(source_kind="other_outlet", source_name="Örnek Gazete",
                     source_url="https://www.gazete.example/ihale-belgeleri-p123",
                     attribution_quote="Örnek Gazete'nin haberine göre; bu ihale belgeleri"), prose, links, REMOVED.url)
    assert (kind, name, url) == ("other_outlet", "Örnek Gazete", "https://www.gazete.example/ihale-belgeleri-p123")
    assert quote.startswith("Örnek Gazete'nin")
    assert dropped == []


def test_ground_claim_drops_a_name_not_in_the_text_and_downgrades_to_unknown():
    kind, name, url, _, dropped = ground_claim(
        SourceResult(source_kind="agency", source_name="Uzak Ajans"), source.prose_of(HTML), [], REMOVED.url)
    assert kind == "unknown" and name == "" and url == ""
    assert any("Uzak Ajans" in d for d in dropped)


def test_ground_claim_drops_a_url_that_was_not_among_the_links_found():
    kind, name, url, _, dropped = ground_claim(
        SourceResult(source_kind="other_outlet", source_name="Örnek Gazete", source_url="https://www.gazete.example/typed-from-memory"),
        source.prose_of(HTML), extract_links(HTML, REMOVED.url), REMOVED.url)
    assert kind == "other_outlet" and name == "Örnek Gazete" and url == ""
    assert any("not among the links" in d for d in dropped)


def test_ground_claim_rejects_the_publishers_own_page_as_a_source():
    kind, _, url, _, dropped = ground_claim(
        SourceResult(source_kind="other_outlet", source_name="Örnek Gazete", source_url="https://www.daily.example.net/gundem/onceki-haber-1"),
        source.prose_of(HTML), ["https://www.daily.example.net/gundem/onceki-haber-1"], REMOVED.url)
    assert url == "" and any("publisher's own page" in d for d in dropped)


def test_ground_claim_normalises_bad_kinds_and_blanks_self():
    kind, name, url, quote, _ = ground_claim(
        SourceResult(source_kind="self", source_name="Örnek Gazete", source_url="", attribution_quote="x"),
        source.prose_of(HTML), [], REMOVED.url)
    assert (kind, name, url, quote) == ("self", "", "", "")
    kind, *_ = ground_claim(SourceResult(source_kind="weird"), "", [], REMOVED.url)
    assert kind == "unknown"


# --- run: itself -------------------------------------------------------------

async def test_live_and_intact_at_its_own_url_is_recorded(net):
    net["probe"] = live(REMOVED.url)
    ev = await source.run(REMOVED, agent(source_kind="self"), FakeSearch())
    assert ev.self_status == "live_intact" and ev.live_at_original_url is True
    assert ev.self_containment >= LIVE_SELF_MIN_CONTAINMENT


async def test_a_soft_404_at_its_own_url_is_live_other_not_intact(net):
    net["probe"] = live(REMOVED.url, "Aradığınız sayfa bulunamadı. " * 30)
    ev = await source.run(REMOVED, agent(source_kind="self"), FakeSearch())
    assert ev.self_status == "live_other" and ev.live_at_original_url is False


async def test_a_dead_own_url_is_dead_or_unreachable(net):
    ev = await source.run(REMOVED, agent(source_kind="self"), FakeSearch())
    assert ev.self_status == "dead_or_unreachable" and ev.live_at_original_url is False


# --- run: cited source -------------------------------------------------------

CITED = "https://www.gazete.example/ihale-belgeleri-p123"


async def test_a_dead_cited_source_raises_the_red_flag(net):
    net["fetch"] = FetchOutcome(dead=[DeadCandidate(url=CITED, title="t", snippet="s", status=404, reason="HTTP 404", source="cited")])
    ev = await source.run(REMOVED, agent(source_kind="other_outlet", source_name="Örnek Gazete", source_url=CITED), FakeSearch())
    assert ev.source_status == "dead"
    assert ev.dead_source is not None and ev.dead_source.url == CITED
    assert ev.dead_source.reason.startswith("cited source")
    assert ev.source_is_dead is True
    assert [h.url for h in net["fetch_hits"]] == [CITED] and net["fetch_hits"][0].source == "cited"
    assert net["exclude_url"] == REMOVED.url


async def test_a_blocked_cited_source_is_not_a_flag(net):
    net["fetch"] = FetchOutcome(unreadable=[UnreadableCandidate(url=CITED, title="t", reason="HTTP 403 (blocked/erroring, not gone)")])
    ev = await source.run(REMOVED, agent(source_kind="other_outlet", source_name="Örnek Gazete", source_url=CITED), FakeSearch())
    assert ev.source_status == "unreadable" and ev.dead_source is None and ev.source_is_dead is False


async def test_a_live_cited_source_is_measured_and_handed_on(net):
    net["fetch"] = FetchOutcome(live=[live(CITED, BODY + " ek")])
    ev = await source.run(REMOVED, agent(source_kind="other_outlet", source_name="Örnek Gazete", source_url=CITED), FakeSearch())
    assert ev.source_status == "live" and ev.live_source.url == CITED
    assert ev.source_containment > 0.5


async def test_a_named_agency_without_a_url_becomes_one_site_scoped_search(net):
    search = FakeSearch([SearchResult(url="https://www.aa.com.tr/tr/gundem/x/1", title="t", snippet="s")])
    ev = await source.run(REMOVED, agent(source_kind="agency", source_name="Anadolu Ajansı"), search)
    assert ev.source_status == "leads_only" and ev.source_host == "aa.com.tr"
    assert search.queries == [(f"site:aa.com.tr {REMOVED.title}", source.CITED_SEARCH_RESULTS, None)]
    assert [h.source for h in ev.extra_hits] == ["cited"]
    assert net["fetch_hits"] is None            # leads are for step 5, not fetched here


async def test_an_unknown_outlet_name_is_not_located(net):
    body = Article(url=REMOVED.url, title=REMOVED.title, content="<p>Yerel Gazete'nin haberine göre olay oldu. " + BODY + "</p>")
    search = FakeSearch()
    ev = await source.run(body, agent(source_kind="other_outlet", source_name="Yerel Gazete"), search)
    assert ev.source_status == "not_located" and search.queries == []


async def test_a_model_failure_keeps_the_self_probe_result(net):
    class Boom:
        async def run(self, prompt, **kwargs):
            raise RuntimeError("empty")
    net["probe"] = live(REMOVED.url)
    ev = await source.run(REMOVED, Boom(), FakeSearch())
    assert ev.live_at_original_url is True and ev.model_failed is True and ev.source_kind == "unknown"


def test_prompt_shows_host_links_and_phrases():
    p = build_prompt(REMOVED, source.prose_of(HTML), extract_links(HTML, REMOVED.url), ["Kaynak: Anadolu Ajansı"])
    assert "daily.example.net" in p and CITED in p and "Kaynak: Anadolu Ajansı" in p


def test_prompt_names_the_fields_and_carries_the_guardrail():
    p = source.prompts.SOURCE_PROMPT.lower()
    for f in SourceResult.model_fields:
        assert f in p
    assert "censored" in p and "todo" not in p


async def test_the_model_call_carries_a_small_output_cap(net):
    seen = {}

    class Recorder:
        async def run(self, prompt, model_settings=None):
            seen["settings"] = model_settings
            class R:
                data = SourceResult(source_kind="self")
            return R()

    await source.run(REMOVED, Recorder(), FakeSearch())
    assert seen["settings"] == {"max_tokens": source.SOURCE_MAX_TOKENS} and source.SOURCE_MAX_TOKENS <= 500


def test_extract_links_ignores_markup_vocabulary_urls():
    # Some publishers' bodies carry itemtype="https://schema.org/Organization" — markup, not a source.
    html = ('<div itemscope itemtype="https://schema.org/Organization"><a href="http://www.w3.org/1999/xhtml">x</a>'
            '<a href="https://wire.example.org/az/xeber/123">Wire</a></div>')
    assert extract_links(html, REMOVED.url) == ["https://wire.example.org/az/xeber/123"]
