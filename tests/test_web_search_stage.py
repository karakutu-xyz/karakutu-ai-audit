"""Stage 3 (web search) — prompt content + the grounding of model-authored evidence.

Deliberately NOT asserted here: semantic correctness of the model's judgement. `TestModel`
returns synthetic data, and prompt quality is evaluated out-of-CI. `fetch_candidates` is
stubbed throughout, so no test opens a socket.
"""

import pytest
from pydantic_ai.models.test import TestModel

from src.api.models import Article
from src.search.base import SearchResult
from src.search.fetch import DeadCandidate, FetchOutcome, LivePage, UnreadableCandidate
from src.search.stub import StubSearchProvider
from src.workflow import web_search
from src.workflow.web_search import (
    PLACEHOLDER_WEB_SEARCH_PROMPT,
    PageMatch,
    WebSearchEvidence,
    WebSearchResult,
)

ARTICLE = Article(
    url="https://t24.com.tr/gundem/rusvet-iddiasi,1",
    title="Ministry contract awarded without tender",
    content="A ministry awarded a public contract to a firm without a competitive tender.",
)


def _prompt() -> str:
    return PLACEHOLDER_WEB_SEARCH_PROMPT.lower()


def live(url: str, title: str = "T", text: str = "body text") -> LivePage:
    return LivePage(url=url, final_url=url, status=200, title=title, text=text)


def dead(url: str, source: str = "web") -> DeadCandidate:
    return DeadCandidate(url=url, title="T", snippet="s", status=404, reason="HTTP 404",
                         source=source)


# --- prompt content ----------------------------------------------------------


def test_prompt_has_no_todo_marker():
    assert "todo" not in _prompt()


def test_prompt_names_the_result_fields():
    p = _prompt()
    for field in WebSearchResult.model_fields:
        assert field in p, f"prompt does not mention result field {field!r}"


def test_prompt_encodes_the_same_story_test():
    p = _prompt()
    for keyword in ("same story", "event", "actors", "facts"):
        assert keyword in p


def test_prompt_warns_about_headline_coincidence():
    p = _prompt()
    assert "headline" in p
    assert "recur" in p


def test_prompt_allows_a_rewritten_headline_on_a_relocation():
    # birgun relocated "İstanbul'da şap alarmı..." to "Şile'de 21 mahalle..." — a real
    # relocation with a rewritten headline. The text must decide, not the headline.
    assert "rewritten headline" in _prompt()


def test_prompt_defines_material_omission_for_relocations():
    p = _prompt()
    assert "covers_key_facts" in p
    assert "omissions" in p
    # The live text is a truncated excerpt, so absence of a fact is NOT omission — the prompt
    # must say so, or the gutted-relocation flag false-fires on our own truncation (a faithful
    # evrensel relocation was flagged POSSIBLE_CENSORSHIP because Emily Gregory/Bannon sat
    # below the 800-char cut).
    assert "excerpt" in p and "truncat" in p
    assert "absence from the excerpt is not evidence of omission" in p


def test_prompt_explains_that_dead_links_are_judged_from_title_and_snippet_only():
    p = _prompt()
    assert "dead links" in p
    assert "cannot read them" in p


def test_prompt_contains_censorship_guardrail():
    # The model gathers evidence; it never reaches a conclusion. It may not even
    # hypothesise censorship — the verdict is chosen in code, by pipeline._decide.
    p = _prompt()
    assert "censored" in p
    assert "not a conclusion" in p or "not reaching a conclusion" in p
    assert "human auditor" in p


# --- run(): grounding model output against what was actually fetched --------


@pytest.fixture
def fetched(monkeypatch):
    """Stub `fetch_candidates`; no test opens a socket.

    The test sets `.outcome` to whatever it wants fetched. Captures the exact hit list
    handed to the fetcher (`box["hits"]`) so tests can assert on how the search passes
    were merged — and on what was NOT re-fetched when earlier steps prefetched it. (The
    removed-URL liveness check and the archive similarity pass no longer live in this
    stage: they are steps 4 and 2 of the pipeline.)
    """
    box = {}

    async def _fake(hits, exclude_url):
        box["exclude_url"] = exclude_url
        box["hits"] = hits
        return box.get("outcome", FetchOutcome())

    monkeypatch.setattr(web_search, "fetch_candidates", _fake)
    return box


class _RecordingProvider:
    """Search provider that records each (query, max_results) and returns per-query hits."""

    def __init__(self, by_query=None, default=None):
        self.by_query = by_query or {}
        self.default = default or []
        self.calls = []

    async def search(self, query, max_results=5, *, language=None):
        self.calls.append((query, max_results, language))
        for needle, hits in self.by_query.items():
            if needle in query:
                return hits[:max_results]
        return self.default[:max_results]


# --- two-pass search: relocation-scoped first, then general ------------------


async def test_search_runs_scoped_then_general_then_phrase_passes(fetched):
    provider = _RecordingProvider()
    agent = web_search.build_agent(TestModel())

    await web_search.run(ARTICLE, provider, agent)

    # Three passes: scoped to the publisher's own domain (relocations are the most common
    # case), then a plain title search, then ONE distinctive prose phrase in quotes —
    # near-identical sibling headlines drown the title search (measured 2026-08-30 QA).
    assert len(provider.calls) == 3
    assert provider.calls[0][0] == "site:t24.com.tr Ministry contract awarded without tender"
    assert provider.calls[1][0] == "Ministry contract awarded without tender"
    phrase_query = provider.calls[2][0]
    assert phrase_query.startswith('"') and phrase_query.endswith('"')
    assert phrase_query.strip('"') in ARTICLE.content, "the phrase must be verbatim prose"


async def test_relocation_pass_hits_lead_and_duplicates_are_merged_out(fetched):
    reloc = SearchResult(url="https://t24.com.tr/haber/moved,1", title="moved", snippet="s")
    other = SearchResult(url="https://birgun.net/haber/x", title="other", snippet="s")
    provider = _RecordingProvider(
        by_query={"site:t24.com.tr": [reloc],
                  # the general pass also surfaces the relocation (dupe) plus another outlet
                  "Ministry": [reloc, other]},
    )
    agent = web_search.build_agent(TestModel())

    await web_search.run(ARTICLE, provider, agent)

    urls = [h.url for h in fetched["hits"]]
    assert urls == ["https://t24.com.tr/haber/moved,1", "https://birgun.net/haber/x"], (
        "relocation hit must lead and the duplicate must be merged out"
    )


async def test_a_failed_scoped_pass_still_runs_the_general_pass(fetched):
    # A provider degrades to [] on failure; an empty relocation pass must not stop the general one.
    other = SearchResult(url="https://birgun.net/haber/x", title="other", snippet="s")
    provider = _RecordingProvider(by_query={"Ministry": [other]}, default=[])
    agent = web_search.build_agent(TestModel())

    await web_search.run(ARTICLE, provider, agent)

    assert len(provider.calls) == 3
    assert [h.url for h in fetched["hits"]] == ["https://birgun.net/haber/x"]


async def test_run_returns_evidence_from_test_model(fetched):
    fetched["outcome"] = FetchOutcome(live=[live("https://example.com/article-1")])
    agent = web_search.build_agent(TestModel())

    result = await web_search.run(ARTICLE, StubSearchProvider(), agent)

    assert isinstance(result, WebSearchEvidence)
    assert isinstance(result.matches, list)
    assert isinstance(result.reasoning, str)


async def test_run_passes_the_removed_url_for_exclusion(fetched):
    agent = web_search.build_agent(TestModel())

    await web_search.run(ARTICLE, StubSearchProvider(), agent)

    assert fetched["exclude_url"] == ARTICLE.url


async def test_nothing_live_and_nothing_dead_short_circuits_without_calling_the_model(fetched):
    # Only unreadable junk came back. That is silence, and silence is not evidence —
    # so there is nothing for the model to judge and no flag to raise.
    fetched["outcome"] = FetchOutcome(
        unreadable=[UnreadableCandidate(url="https://x/y.pdf", title="t", reason="not HTML")],
    )
    agent = web_search.build_agent(TestModel())

    result = await web_search.run(ARTICLE, StubSearchProvider(), agent)

    assert result.matches == []
    assert result.dead_same_story == []
    assert result.relocated_to is None
    assert result.unreadable_count == 1
    # The wording names it as a search-quality problem, not evidence about the article.
    assert "search-quality problem" in result.reasoning


# --- _ground() ---------------------------------------------------------------


def test_a_hallucinated_match_url_is_dropped():
    # Real regression: the model emitted `bbc.com.tr/turkce/...` (a domain that does not
    # resolve) when the fetched page was `bbc.com/turkce/...`, and the pipeline pasted that
    # fabricated URL into findings as evidence.
    outcome = FetchOutcome(live=[live("https://www.bbc.com/turkce/articles/x")])
    raw = WebSearchResult(
        matches=[PageMatch(url="https://www.bbc.com.tr/turkce/articles/x")],
        reasoning="covers the same story",
    )

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    assert ev.matches == []


def test_a_real_match_is_rewritten_to_the_url_actually_fetched():
    outcome = FetchOutcome(live=[live("https://www.bbc.com/turkce/articles/x", "BBC")])
    raw = WebSearchResult(
        # same page, but the model typed it without www. and with a trailing slash
        matches=[PageMatch(url="http://bbc.com/turkce/articles/x/")],
        reasoning="same story",
    )

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    assert [m.url for m in ev.matches] == ["https://www.bbc.com/turkce/articles/x"]


def test_a_same_publisher_live_copy_is_detected_as_a_relocation():
    # Relocation is decided in CODE (same host, different path), not by the model.
    outcome = FetchOutcome(
        live=[live("https://t24.com.tr/haber/rusvet-iddiasi,1", text=ARTICLE.content)])
    raw = WebSearchResult(
        matches=[PageMatch(url="https://t24.com.tr/haber/rusvet-iddiasi,1", covers_key_facts=True)],
        reasoning="republished",
    )

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    assert ev.relocated_to is not None
    assert ev.relocated_to.url == "https://t24.com.tr/haber/rusvet-iddiasi,1"
    assert ev.relocation_covers_key_facts is True


def test_a_different_outlet_is_not_a_relocation():
    outcome = FetchOutcome(live=[live("https://www.birgun.net/haber/x")])
    raw = WebSearchResult(matches=[PageMatch(url="https://www.birgun.net/haber/x")],
                          reasoning="other outlet")

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    assert ev.relocated_to is None
    assert len(ev.matches) == 1


def test_a_gutted_relocation_carries_its_omissions_through():
    outcome = FetchOutcome(
        live=[live("https://t24.com.tr/haber/rusvet-iddiasi,1", text=ARTICLE.content)])
    raw = WebSearchResult(
        matches=[PageMatch(
            url="https://t24.com.tr/haber/rusvet-iddiasi,1",
            covers_key_facts=False,
            omissions=["the bribery allegation", ""],   # blanks must be discarded
        )],
        reasoning="republished without the allegation",
    )

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    assert ev.relocation_covers_key_facts is False
    assert ev.omissions == ["the bribery allegation"]


def test_dead_same_story_urls_are_grounded_against_real_dead_candidates():
    outcome = FetchOutcome(dead=[dead("https://gazeteduvar.com.tr/x")])
    raw = WebSearchResult(
        dead_same_story=["https://gazeteduvar.com.tr/x", "https://invented.example/nope"],
        reasoning="one dead copy looks like the same story",
    )

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    # The invented one is dropped — a flag must never rest on a URL we never saw.
    assert [d.url for d in ev.dead_same_story] == ["https://gazeteduvar.com.tr/x"]


def test_build_agent_uses_the_web_search_prompt():
    agent = web_search.build_agent(TestModel())

    # 0.0.49: prompt is registered as a system prompt on the agent.
    assert PLACEHOLDER_WEB_SEARCH_PROMPT in agent._system_prompts


# --- model failure: retry smaller, then fail loudly --------------------------


async def test_the_model_call_retries_with_a_smaller_prompt(fetched, monkeypatch):
    """The empty-response failure is prompt-size driven, so a failure must retry smaller.
    Regression: censored case 3 hard-failed with 2 live pages AND a dead same-story copy in
    hand — real evidence thrown away because the model returned empty."""
    fetched["outcome"] = FetchOutcome(live=[live(f"https://n{i}.example/a") for i in range(3)])
    prompt_sizes = []

    class FlakyAgent:
        async def run(self, prompt, **kwargs):
            prompt_sizes.append(len(prompt))
            if len(prompt_sizes) < 3:      # fail the first two, succeed on the smallest
                raise RuntimeError("Received empty model response")
            class R:
                data = WebSearchResult(matches=[], reasoning="ok on the small prompt")
            return R()

    result = await web_search.run(ARTICLE, StubSearchProvider(), FlakyAgent())

    assert len(prompt_sizes) == 3, "should have retried twice"
    assert prompt_sizes == sorted(prompt_sizes, reverse=True), "each retry must be smaller"
    assert result.reasoning == "ok on the small prompt"


async def test_a_total_model_failure_raises_rather_than_faking_a_result(fetched):
    # If every attempt fails the audit did NOT run. It must raise so the pipeline can say
    # AUDIT_FAILED — never quietly return "found nothing".
    fetched["outcome"] = FetchOutcome(live=[live("https://n.example/a")])

    class DeadAgent:
        async def run(self, prompt):
            raise RuntimeError("Received empty model response")

    with pytest.raises(web_search.WebSearchModelFailure):
        await web_search.run(ARTICLE, StubSearchProvider(), DeadAgent())


# --- leads from earlier steps: extra_hits + prefetched ------------------------
#
# The archive similarity pass (step 2) and the cited-source check (step 4) run BEFORE this
# stage and hand over what they found (`extra_hits`, leading the merged list) and what they
# already fetched (`prefetched`, never fetched again). The archive-dead suppression rule
# and its web-corroboration upgrade must survive the hand-over unchanged.


def archive_hit(url):
    return SearchResult(url=url, title="t", snippet="s", source="archive")


async def test_extra_hits_lead_the_merged_hits(fetched):
    agent = web_search.build_agent(TestModel())
    lead = archive_hit("https://t24.com.tr/haber/relocated,9")
    await web_search.run(ARTICLE, StubSearchProvider(), agent, extra_hits=[lead])
    assert fetched["hits"][0].url == lead.url
    assert fetched["hits"][0].source == "archive"


async def test_prefetched_pages_are_not_fetched_again(fetched):
    agent = web_search.build_agent(TestModel())
    lead = archive_hit("https://example.com/article-1")   # also returned by the stub provider
    pre = FetchOutcome(live=[live(lead.url)])
    result = await web_search.run(ARTICLE, StubSearchProvider(), agent, prefetched=pre, extra_hits=[lead])
    assert all(h.url != lead.url for h in fetched["hits"])   # not passed to fetch_candidates again
    assert result.live_pages_read >= 1                       # but it still counts as read


async def test_a_prefetched_archive_dead_lead_is_upgraded_when_a_web_pass_returns_it(fetched):
    # StubSearchProvider returns example.com/article-1..3, so an archive-sourced dead candidate at
    # article-2 is independently web-indexed: its 404 regains evidentiary standing.
    agent = web_search.build_agent(TestModel(custom_result_args={
        "matches": [], "dead_same_story": ["https://example.com/article-2"], "reasoning": "r"}))
    pre = FetchOutcome(dead=[dead("https://example.com/article-2", source="archive")])
    fetched["outcome"] = FetchOutcome(live=[live("https://example.com/article-1")])
    result = await web_search.run(ARTICLE, StubSearchProvider(), agent, prefetched=pre,
                                  extra_hits=[archive_hit("https://example.com/article-2")])
    assert [d.url for d in result.dead_same_story] == ["https://example.com/article-2"]


async def test_a_prefetched_archive_dead_lead_stays_suppressed_without_web_corroboration(fetched):
    agent = web_search.build_agent(TestModel(custom_result_args={
        "matches": [], "dead_same_story": ["https://archive-only.net/gone"], "reasoning": "r"}))
    pre = FetchOutcome(dead=[dead("https://archive-only.net/gone", source="archive")])
    fetched["outcome"] = FetchOutcome(live=[live("https://example.com/article-1")])
    result = await web_search.run(ARTICLE, StubSearchProvider(), agent, prefetched=pre,
                                  extra_hits=[archive_hit("https://archive-only.net/gone")])
    assert result.dead_same_story == []
    assert "set aside" in result.reasoning


async def test_a_cited_lead_keeps_its_source_and_its_dead_link_counts(fetched):
    agent = web_search.build_agent(TestModel(custom_result_args={
        "matches": [], "dead_same_story": ["https://aa.com.tr/tr/x/1"], "reasoning": "r"}))
    fetched["outcome"] = FetchOutcome(dead=[dead("https://aa.com.tr/tr/x/1", source="cited")])
    result = await web_search.run(ARTICLE, StubSearchProvider(), agent,
                                  extra_hits=[SearchResult(url="https://aa.com.tr/tr/x/1", title="t", snippet="s", source="cited")])
    assert [d.url for d in result.dead_same_story] == ["https://aa.com.tr/tr/x/1"]


# --- source-aware dead evidence (the archive can find copies, not bury them) --
#
# Measured (2026-08-28/29 run over 150 undecided audits): all 6 POSSIBLE_CENSORSHIP flags
# were false, and the dead "same-story copies" they rested on were proposed by the
# archive's similarity pass. The archive is a corpus OF REMOVED articles, so its
# candidates are structurally biased toward 404s — an archive candidate answering 404
# proves nothing about an independent copy. Archive candidates keep FULL standing as live
# matches/relocations (96% measured relocation recall); they just cannot prove a copy died.


def test_an_archive_sourced_dead_candidate_never_becomes_flag_evidence():
    url = "https://bizim.media/az/cemiyyet/500000/basqa-silinmis-xeber"
    outcome = FetchOutcome(dead=[dead(url, source="archive")])
    raw = WebSearchResult(dead_same_story=[url], reasoning="one dead copy looks the same")

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    assert ev.dead_same_story == []
    # The suppression must be said out loud, not silently swallowed: the findings must not
    # imply "no copy was demonstrably gone" without saying what was set aside and why.
    assert "archive" in ev.reasoning


def test_a_web_sourced_dead_candidate_is_still_flag_evidence():
    # The Bilal-Erdoğan-style true positive: a real artigercek.com 404 surfaced by WEB
    # search on an article-shaped URL. That must keep producing dead evidence.
    url = "https://artigercek.com/guncel/bilal-erdogan-haberi"
    outcome = FetchOutcome(dead=[dead(url)])   # source defaults to "web"
    raw = WebSearchResult(dead_same_story=[url], reasoning="same story, now gone")

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    assert [d.url for d in ev.dead_same_story] == [url]


def test_mixed_sources_keep_only_the_web_dead(): 
    web_url = "https://artigercek.com/guncel/gone-story"
    arc_url = "https://bizim.media/az/cemiyyet/500000/removed-row"
    outcome = FetchOutcome(dead=[dead(arc_url, source="archive"), dead(web_url)])
    raw = WebSearchResult(dead_same_story=[web_url, arc_url], reasoning="two dead copies")

    ev = web_search._ground(raw, outcome, ARTICLE, hits_searched=5)

    assert [d.url for d in ev.dead_same_story] == [web_url]
    assert "archive" in ev.reasoning


def test_prompt_dead_links_exclude_archive_sourced_candidates():
    # An archive-sourced dead link can never become evidence, so showing it to the model
    # only invites doomed citations and crowds a genuine web 404 out of the capped list.
    outcome = FetchOutcome(
        live=[live("https://n.example/a")],
        dead=[dead("https://bizim.media/az/cemiyyet/500000/removed-row", source="archive"),
              dead("https://artigercek.com/guncel/gone-story")],
    )

    prompt = web_search._build_prompt(ARTICLE, outcome)

    assert "artigercek.com/guncel/gone-story" in prompt
    assert "bizim.media/az/cemiyyet/500000" not in prompt


async def test_only_archive_dead_and_nothing_live_short_circuits_without_model(fetched):
    # Nothing live and the only dead links are archive-sourced: there is nothing a model
    # could establish, so no model call — and no flag.
    fetched["outcome"] = FetchOutcome(
        dead=[dead("https://bizim.media/az/cemiyyet/500000/removed-row", source="archive")],
    )

    class PoisonAgent:
        async def run(self, prompt):
            raise AssertionError("the model must not be called")

    result = await web_search.run(ARTICLE, StubSearchProvider(), PoisonAgent())

    assert result.dead_same_story == []
    assert result.matches == []
    assert "archive" in result.reasoning


# --- language routing through the search passes -------------------------------


AZ_ARTICLE = Article(
    url=("https://bizim.media/az/cemiyyet/591993/"
         "qarli-havaya-gore-mektebler-balanacaq-resmi-aiqlama"),
    title="Qarlı havaya görə məktəblər bağlanacaq",
    content="Təhsil Nazirliyi qarlı hava şəraiti ilə bağlı məktəblərin bağlanacağını açıqladı.",
)


async def test_the_language_guess_is_passed_to_both_search_passes(fetched):
    # 35/75 INCONCLUSIVE audits in the measured run were Azerbaijani articles searched
    # with language=tr. The guess from the removed URL must reach BOTH passes.
    provider = _RecordingProvider()

    await web_search.run(AZ_ARTICLE, provider, web_search.build_agent(TestModel()))

    assert [c[2] for c in provider.calls] == ["az", "az", "az"]


async def test_no_language_guess_passes_none_so_providers_use_their_default(fetched):
    provider = _RecordingProvider()

    await web_search.run(ARTICLE, provider, web_search.build_agent(TestModel()))

    assert [c[2] for c in provider.calls] == [None, None, None]


# --- measured relocation: containment + sibling demotion ---------------------
#
# Measurement replaced judgement: 8 of 12 recent LIKELY_MOVED verdicts were same-publisher
# SIBLING articles (same news cycle, different article). A same-publisher candidate is now
# measured (token-shingle containment, src/search/textmatch.py) before it may become
# `relocated_to`; a measurably-different article is demoted to `related_same_publisher`.

from src.workflow.pipeline import _decide  # noqa: E402  (tier checks on grounded evidence)

_RUSVET_TEXT = (
    "Savcılık, belediye başkanı hakkında ihale sürecinde rüşvet iddiasıyla soruşturma "
    "başlattı. İddiaya göre 12 milyon liralık ihale usulsüz biçimde verildi ve denetim "
    "raporları gizlendi. Tanık ifadelerinde ihale komisyonunun baskı altında karar aldığı "
    "öne sürüldü. Belediye meclisi üyeleri sürecin bağımsız denetçiler tarafından "
    "incelenmesini istedi. Soruşturma kapsamında üç belediye çalışanının ifadesine "
    "başvurulduğu öğrenildi. Başkanın avukatı iddiaları reddederek sürecin siyasi "
    "olduğunu savundu."
)
# Same publisher, same news cycle — but measurably a DIFFERENT article.
_SIBLING_TEXT = (
    "Belediye meclisi yeni dönem bütçe görüşmelerine başladı. Toplantıda ulaşım "
    "yatırımları ve su tarifeleri ele alındı. Üyeler mahalle projeleri için ek kaynak "
    "ayrılmasını talep etti. Görüşmelerin hafta sonuna kadar sürmesi bekleniyor. Bütçe "
    "oylamasının gelecek hafta yapılacağı açıklandı."
)

REMOVED_EVRENSEL = Article(
    url="https://www.evrensel.net/yazi/98980/washington-da-catlak-sesler",
    title="Washington'da çatlak sesler",
    content=_RUSVET_TEXT,
)


def _match(url: str) -> WebSearchResult:
    return WebSearchResult(matches=[PageMatch(url=url)], reasoning="same story")


def test_a_verbatim_same_publisher_copy_measures_fully_contained():
    url = "https://www.evrensel.net/yazi/98981/washington-da-catlak-sesler"
    extra = " Haber daha sonra ek açıklamalarla birlikte yeniden yayımlandı."
    outcome = FetchOutcome(live=[live(url, text=_RUSVET_TEXT + extra)])

    ev = web_search._ground(_match(url), outcome, REMOVED_EVRENSEL, hits_searched=5)

    assert ev.relocated_to is not None
    assert ev.relocation_old_in_new == 1.0
    assert ev.relocation_new_in_old is not None and ev.relocation_new_in_old < 1.0
    assert ev.related_same_publisher == []


def test_a_same_publisher_sibling_is_demoted_not_a_relocation():
    url = "https://www.evrensel.net/haber/5991608/butce-gorusmeleri"
    outcome = FetchOutcome(live=[live(url, text=_SIBLING_TEXT)])

    ev = web_search._ground(_match(url), outcome, REMOVED_EVRENSEL, hits_searched=5)

    assert ev.relocated_to is None
    assert ev.matches == [], "a measured sibling must not stand as a same-story match"
    assert [r.url for r in ev.related_same_publisher] == [url]


def test_a_demoted_sibling_does_not_block_a_real_relocation_behind_it():
    sibling = "https://www.evrensel.net/haber/5991608/butce-gorusmeleri"
    reloc = "https://www.evrensel.net/yazi/98981/washington-da-catlak-sesler"
    outcome = FetchOutcome(live=[
        live(sibling, text=_SIBLING_TEXT), live(reloc, text=_RUSVET_TEXT),
    ])
    raw = WebSearchResult(matches=[PageMatch(url=sibling), PageMatch(url=reloc)],
                          reasoning="two same-publisher pages")

    ev = web_search._ground(raw, outcome, REMOVED_EVRENSEL, hits_searched=5)

    assert ev.relocated_to is not None and ev.relocated_to.url == reloc
    assert [m.url for m in ev.matches] == [reloc]
    assert [r.url for r in ev.related_same_publisher] == [sibling]


def test_a_cross_publisher_match_is_never_measured_or_demoted():
    # Different outlets legitimately rewrite; text distance says nothing there.
    url = "https://www.birgun.net/haber/ayni-olay-farkli-anlatim"
    outcome = FetchOutcome(live=[live(url, text=_SIBLING_TEXT)])

    ev = web_search._ground(_match(url), outcome, REMOVED_EVRENSEL, hits_searched=5)

    assert [m.url for m in ev.matches] == [url]
    assert ev.related_same_publisher == []
    assert ev.relocation_old_in_new is None


def test_removed_content_as_raw_html_is_prose_extracted_before_measurement():
    # The archive hands `content` as RAW HTML (measured: audit 230110's archived content
    # embeds evrensel's subscription-CTA markup and SVG paths, which depressed a verbatim
    # republication to old_in_new 0.40). Both sides go through prose extraction.
    paragraphs = "".join(f"<p>{sent.strip()}.</p>" for sent in _RUSVET_TEXT.split(". ") if sent)
    html_removed = Article(
        url=REMOVED_EVRENSEL.url, title=REMOVED_EVRENSEL.title,
        content=('<div property="articleBody">' + paragraphs
                 + '<div class="evr-sub-cta">Google Play\'den İndir</div></div>'),
    )
    url = "https://www.evrensel.net/yazi/98981/washington-da-catlak-sesler"
    outcome = FetchOutcome(live=[live(url, text=_RUSVET_TEXT)])

    ev = web_search._ground(_match(url), outcome, html_removed, hits_searched=5)

    assert ev.relocated_to is not None
    assert ev.relocation_old_in_new is not None and ev.relocation_old_in_new > 0.95


def test_an_unmeasurable_fit_keeps_the_relocation_with_no_numbers():
    # No usable text on the removed side: measurement is impossible, so the candidate keeps
    # today's standing (LIKELY_MOVED downstream) rather than being demoted on no evidence.
    unmeasurable = Article(url=REMOVED_EVRENSEL.url, title="t", content="a b c 12")
    url = "https://www.evrensel.net/yazi/98981/washington-da-catlak-sesler"
    outcome = FetchOutcome(live=[live(url, text=_SIBLING_TEXT)])

    ev = web_search._ground(_match(url), outcome, unmeasurable, hits_searched=5)

    assert ev.relocated_to is not None
    assert ev.relocation_old_in_new is None
    assert ev.relocation_new_in_old is None


def test_recorded_relocation_shapes_are_never_demoted_to_sibling():
    """The labelled-sample guarantee: the auditors' recorded relocations (t24 verbatim
    re-section, evrensel faithful-but-shortened re-publish) must come out MOVED or
    LIKELY_MOVED — never demoted to a sibling, never INCONCLUSIVE."""
    from src.api.models import Verdict

    # t24: /gundem/ -> /haber/, verbatim text under the same article id.
    t24_removed = Article(url="https://t24.com.tr/gundem/rusvet-iddiasi,1222402",
                          title="t", content=_RUSVET_TEXT)
    t24_new = "https://t24.com.tr/haber/rusvet-iddiasi,1222402"
    ev = web_search._ground(
        _match(t24_new), FetchOutcome(live=[live(t24_new, text=_RUSVET_TEXT)]),
        t24_removed, hits_searched=5,
    )
    assert ev.relocated_to is not None and ev.related_same_publisher == []
    verdict, _ = _decide(ev)
    assert verdict is Verdict.MOVED

    # evrensel: adjacent-id re-publish that kept only the opening paragraphs (faithful but
    # shortened) — a partial fit, still a relocation for a human to check.
    shortened = " ".join(_RUSVET_TEXT.split(". ")[:2]) + "."
    ev = web_search._ground(
        _match("https://www.evrensel.net/yazi/98981/washington-da-catlak-sesler"),
        FetchOutcome(live=[live("https://www.evrensel.net/yazi/98981/washington-da-catlak-sesler",
                                text=shortened)]),
        REMOVED_EVRENSEL, hits_searched=5,
    )
    assert ev.relocated_to is not None and ev.related_same_publisher == []
    verdict, _ = _decide(ev)
    assert verdict in (Verdict.MOVED, Verdict.LIKELY_MOVED)


# --- the phrase pass (third web pass) ----------------------------------------
#
# Measured (2026-08-30 QA of the 150-case run): 88/150 INCONCLUSIVE, and in 5 of 6 sampled
# cases a copy verifiably existed on major outlets that the bare-title search never
# surfaced — near-identical sibling headlines drown the story. One distinctive quoted
# phrase from the prose cuts through the title collision.


async def test_no_phrase_pass_when_the_content_is_too_short(fetched):
    art = Article(url=ARTICLE.url, title=ARTICLE.title, content="Too short.")
    provider = _RecordingProvider()

    await web_search.run(art, provider, web_search.build_agent(TestModel()))

    assert len(provider.calls) == 2, "the phrase pass must degrade away, not send junk"


async def test_hits_merge_leads_site_general_phrase_and_cap_at_ten(fetched):
    lead = archive_hit("https://t24.com.tr/haber/arc-1")
    site = [SearchResult(url=f"https://t24.com.tr/haber/s{i}", title="s", snippet="s")
            for i in range(4)]
    general = [SearchResult(url=f"https://g{i}.example/a", title="g", snippet="s")
               for i in range(4)]
    phrase = [SearchResult(url=f"https://p{i}.example/a", title="p", snippet="s")
              for i in range(4)]
    provider = _RecordingProvider(by_query={"site:": site, '"': phrase}, default=general)

    await web_search.run(ARTICLE, provider, web_search.build_agent(TestModel()), extra_hits=[lead])

    urls = [h.url for h in fetched["hits"]]
    assert len(urls) == 10, "the merged candidate cap is 10"
    assert urls[0] == "https://t24.com.tr/haber/arc-1", "the leads from earlier steps come first"
    assert urls[1:5] == [h.url for h in site]
    assert urls[5:9] == [h.url for h in general]
    assert urls[9] == "https://p0.example/a", "the phrase pass fills the tail"


async def test_every_rung_of_the_ladder_carries_a_small_output_cap(fetched):
    fetched["outcome"] = FetchOutcome(live=[live("https://example.com/article-1")])
    seen = []

    class Recorder:
        async def run(self, prompt, model_settings=None):
            seen.append(model_settings)
            if len(seen) < 3:
                raise RuntimeError("empty response")
            class R:
                data = WebSearchResult(matches=[], dead_same_story=[], reasoning="r")
            return R()

    await web_search.run(ARTICLE, StubSearchProvider(), Recorder())
    assert seen == [{"max_tokens": web_search.WEB_SEARCH_MAX_TOKENS}] * 3
    assert web_search.WEB_SEARCH_MAX_TOKENS <= 800


async def test_a_total_model_failure_names_the_type_but_never_the_raw_reply(fetched):
    """The failure message ends up in AUDIT_FAILED findings, returned by an
    unauthenticated endpoint — it must carry the exception TYPE only."""
    fetched["outcome"] = FetchOutcome(live=[live("https://n.example/a")])

    class LeakyAgent:
        async def run(self, prompt, **kw):
            raise RuntimeError("upstream http://10.0.0.7/v1 replied: secret-model-body")

    with pytest.raises(web_search.WebSearchModelFailure) as info:
        await web_search.run(ARTICLE, StubSearchProvider(), LeakyAgent())
    assert "(last: RuntimeError)" in str(info.value)
    assert "secret-model-body" not in str(info.value) and "10.0.0.7" not in str(info.value)
