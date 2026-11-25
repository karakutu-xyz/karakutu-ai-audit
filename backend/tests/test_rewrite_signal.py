"""Invented word sets and pages for the dated same-publisher rewrite signal."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from audit.clients.models import SimilarArticle
from audit.domain import Article
from audit.search.boilerplate import own_text_words
from audit.search.fetch import (
    DeadCandidate, FetchOutcome, LivePage, UnreadableCandidate, normalize_url,
)
from audit.search.rewrite import (
    date_gap_days, distinctive_words, headline_words, is_rewrite, title_overlap,
    word_overlap,
)
from audit.workflow import similarity, web_search


OLD_URL = "https://paper.example/invented-council-budget"
NEW_URL = "https://paper.example/invented-council-budget-update"
TITLE = "Invented council reservoir budget approved"
WORDS = [f"detail{n:03}" for n in range(30)]


def test_headline_turkish_casefold():
    assert headline_words("İÇME SUYU BAŞLATTI", "") == frozenset({
        "içme", "suyu", "başlatti",
    })
    assert title_overlap("İÇME SUYU BAŞLATTI", "", "içme suyu başlattı", "") == (1, 1)


def test_headline_slug_fallback_and_digit_removal():
    url = "https://paper.example/older/123-budget-%C4%B0%C3%87ME-456/"
    assert headline_words("!?", url) == frozenset({"budget", "içme"})
    assert headline_words("Actual headline", url) == frozenset({"actual", "headline"})
    assert headline_words("", "https://paper.example/123456/") == frozenset()
    assert headline_words("", "https://paper.example/") == frozenset()


@pytest.mark.parametrize(("old", "new", "expected"), [
    ("alpha bravo", "bravo charlie delta", (0.5, 1 / 3)),
    ("", "bravo", (None, 0)),
    ("alpha", "", (0, None)),
    ("", "", (None, None)),
])
def test_title_overlap_directions_and_empty_sides(old, new, expected):
    assert title_overlap(old, "", new, "") == expected


def test_distinctive_words_and_whole_candidate_overlap():
    assert distinctive_words("a the four FIFTH sixth fifth") == frozenset({"fifth", "sixth"})
    assert word_overlap(frozenset({"fifth", "sixth"}), "new footer FIFTH") == (0.5, 2)
    assert word_overlap(frozenset({"fifth"}), "") == (0, 1)
    assert word_overlap(frozenset(), "fifth") == (None, 0)


def test_boilerplate_words_are_excluded_from_own_shingles():
    body = " ".join(WORDS)
    footer = "invented subscription newsletter navigation"
    assert own_text_words(body + " " + footer, frozenset({footer})) == frozenset(WORDS)
    # The candidate's whole text counts, including words in its footer.
    assert word_overlap(own_text_words(body, frozenset()), footer + " detail000") == (1 / 30, 30)


def test_boilerplate_cap_and_empty_cleaning_fall_back():
    text = " ".join(WORDS)
    assert own_text_words(text, frozenset({" ".join(WORDS[:20])})) == distinctive_words(text)
    assert own_text_words(text, frozenset({text})) == distinctive_words(text)
    assert own_text_words("", frozenset()) == frozenset()
    assert own_text_words("Brief update", frozenset()) == frozenset({"brief", "update"})


@pytest.mark.parametrize(("old", "new", "archive", "expected"), [
    ("2031-03-12T23:59:00+05:00", "2031-03-14T00:01:00", 9, 2),
    ("2031-03-14T00:01:00", "2031-03-12T23:59:00Z", 9, 2),
    ("2031-03-12", "2031-03-12", None, 0),
    ("2031-03-12", "invalid", 1, 1),
    (None, "2031-03-12", 2, 2),
    ("2031-03-12", None, None, None),
    (None, None, None, None),
    (None, None, 0, 0),
])
def test_date_gap_uses_calendar_dates_then_archive(old, new, archive, expected):
    old = datetime.fromisoformat(old) if old else None
    assert date_gap_days(old, new, archive) == expected


@pytest.mark.parametrize(("changes", "expected"), [
    ({}, True),
    ({"title_overlap": 0.349999}, False),
    ({"word_overlap": 0.349999}, False),
    ({"date_gap_days": 3}, False),
    ({"date_gap_days": None}, False),
    ({"title_overlap": None}, False),
    ({"word_overlap": None}, False),
    ({"own_words": 19}, False),
    ({"title_overlap_reverse": None}, True),
    ({"final_url": "https://paper.example/"}, False),
    ({"final_url": "https://paper.example/section"}, False),
    ({"final_url": None}, True),
    ({"final_url": NEW_URL + "/?view=reader"}, True),
])
def test_text_rule_inclusive_bars(changes, expected):
    values = dict(title_overlap=0.35, title_overlap_reverse=0.1,
                  word_overlap=0.35, own_words=20, date_gap_days=2,
                  headline_in_text=None, requested_url=NEW_URL,
                  final_url=NEW_URL)
    assert is_rewrite(**(values | changes)) is expected


@pytest.mark.parametrize(("changes", "expected"), [
    ({}, True),
    ({"title_overlap": 0.799999}, False),
    ({"title_overlap_reverse": 0.799999}, False),
    ({"title_overlap_reverse": None}, False),
    ({"date_gap_days": 2}, False),
    ({"date_gap_days": None}, False),
    ({"own_words": 0}, True),
    ({"own_words": 20}, False),
    ({"headline_in_text": 0.799999}, False),
    ({"headline_in_text": None}, False),
    ({"final_url": "https://paper.example/"}, False),
    ({"final_url": None}, True),
    ({"final_url": NEW_URL + "/?view=reader"}, True),
])
def test_stub_rule_both_directions_and_tighter_gap(changes, expected):
    values = dict(title_overlap=0.80, title_overlap_reverse=0.80,
                  word_overlap=None, own_words=19, date_gap_days=1,
                  headline_in_text=0.80, requested_url=NEW_URL, final_url=NEW_URL)
    assert is_rewrite(**(values | changes)) is expected


def removed_article():
    return Article(url=OLD_URL, title=TITLE, content=" ".join(WORDS),
                   published_at=datetime(2031, 3, 12))


def archive_candidate(**changes):
    return SimilarArticle(**(dict(id=1, link=NEW_URL, title=TITLE,
                                 publish_date="2031-03-14") | changes))


def live_page(**changes):
    return LivePage(**(dict(url=NEW_URL, final_url=NEW_URL, status=200,
                           title=TITLE, text=" ".join(reversed(WORDS))) | changes))


async def run_similarity(monkeypatch, candidates, fetched, *, article=None):
    async def lookup(url, limit):
        return candidates

    async def fetch(hits, exclude_url):
        return fetched

    monkeypatch.setattr(similarity, "fetch_candidates", fetch)
    return await similarity.run(
        article or removed_article(),
        client=SimpleNamespace(enabled=True, find_similar_by_url=lookup),
    )


@pytest.mark.parametrize(("fields", "expected_gap", "expected_rewrite"), [
    ({}, None, False),
    ({"date_proximity_days": 0}, 0, True),
    ({"date_proximity_days": 2}, 2, True),
    ({"date_proximity_days": 3}, 3, False),
    ({"publish_date": "2031-03-14"}, 2, True),
    ({"publish_date": "invalid"}, None, False),
])
async def test_archive_date_gap_must_actually_be_sent(
    monkeypatch, fields, expected_gap, expected_rewrite,
):
    candidate = archive_candidate(**({"publish_date": None} | fields))
    evidence = await run_similarity(monkeypatch, [candidate], FetchOutcome(live=[live_page()]))
    measured = evidence.candidates[0]
    assert measured.date_gap_days == expected_gap
    assert measured.rewrite is expected_rewrite
    assert (evidence.rewrite is not None) is expected_rewrite


@pytest.mark.parametrize("title_source", ["archive", "slug"])
async def test_similarity_headline_source_order(monkeypatch, title_source):
    article = removed_article().model_copy(update={"title": "invented council budget"})
    candidate = archive_candidate(title=article.title if title_source == "archive" else "")
    result = await run_similarity(monkeypatch, [candidate],
                                  FetchOutcome(live=[live_page(title=candidate.title)]),
                                  article=article)
    assert result.rewrite is not None
    assert result.rewrite.title_overlap == 1


async def test_verbatim_is_never_a_rewrite(monkeypatch):
    result = await run_similarity(monkeypatch, [archive_candidate()],
                                  FetchOutcome(live=[live_page(text=" ".join(WORDS))]))
    assert result.candidates[0].fit == "verbatim"
    assert result.candidates[0].rewrite is False
    assert result.rewrite is None
    assert result.relocated_to.url == NEW_URL


@pytest.mark.parametrize("kind", ["cross_publisher", "off_host_redirect", "dead", "unreadable"])
async def test_only_live_requested_and_final_same_publisher_pages_are_measured(monkeypatch, kind):
    url = ("https://elsewhere.example/invented-council-budget"
           if kind == "cross_publisher" else NEW_URL)
    candidate = archive_candidate(link=url)
    if kind == "dead":
        fetched = FetchOutcome(dead=[DeadCandidate(
            url=url, title=TITLE, snippet="", status=404, reason="HTTP 404",
        )])
    elif kind == "unreadable":
        fetched = FetchOutcome(unreadable=[
            UnreadableCandidate(url=url, title=TITLE, reason="blocked"),
        ])
    else:
        final = "https://elsewhere.example/redirected" if kind == "off_host_redirect" else url
        fetched = FetchOutcome(live=[live_page(url=url, final_url=final)])
    result = await run_similarity(monkeypatch, [candidate], fetched)
    measured = result.candidates[0]
    for field in ("title_overlap", "title_overlap_reverse", "headline_in_text",
                  "word_overlap", "own_words",
                  "date_gap_days", "old_in_new", "new_in_old"):
        assert getattr(measured, field) is None
    assert measured.rewrite is False
    assert result.rewrite is None


async def test_candidates_record_why_a_page_could_not_be_read(monkeypatch):
    urls = [f"{NEW_URL}/{i}" for i in range(4)]
    fetched = FetchOutcome(
        unreadable=[UnreadableCandidate(url=urls[0], title=TITLE,
                                        reason="HTTP 403 (blocked/erroring, not gone)"),
                    UnreadableCandidate(url=urls[1], title=TITLE, reason="x" * 300)],
        dead=[DeadCandidate(url=urls[2], title=TITLE, snippet="", status=404, reason="HTTP 404")],
        live=[live_page(url=urls[3], final_url=urls[3])],
    )
    result = await run_similarity(
        monkeypatch, [archive_candidate(id=i, link=u) for i, u in enumerate(urls)], fetched)
    by_url = {c.url: c for c in result.candidates}
    assert (by_url[urls[0]].status, by_url[urls[0]].reason) == (
        "unreadable", "HTTP 403 (blocked/erroring, not gone)")
    assert by_url[urls[1]].reason == "x" * 120
    assert (by_url[urls[2]].status, by_url[urls[2]].reason) == ("dead", "HTTP 404")
    assert by_url[urls[3]].status == "live" and by_url[urls[3]].reason is None


async def test_best_rewrite_ranks_words_then_headline(monkeypatch):
    candidates = [archive_candidate(id=i, link=f"{NEW_URL}/{i}") for i in range(3)]
    pages = [live_page(url=c.link, final_url=c.link, text=" ".join(reversed(WORDS[:count])))
             for c, count in zip(candidates, [15, 24, 24])]
    candidates[1].title = "Invented council reservoir other headline"
    evidence = await run_similarity(monkeypatch, candidates, FetchOutcome(live=pages))
    assert all(c.rewrite for c in evidence.candidates)
    assert evidence.rewrite.url == candidates[2].link
    assert "same-publisher rewrite" in evidence.reason


async def test_best_stub_rewrite_ranks_headline(monkeypatch):
    article = removed_article().model_copy(update={"content": "Brief update"})
    candidates = [archive_candidate(id=i, link=f"{NEW_URL}/{i}", date_proximity_days=1,
                                    publish_date=None) for i in range(2)]
    candidates[0].title = "Invented council reservoir budget changes"
    evidence = await run_similarity(monkeypatch, candidates, FetchOutcome(live=[
        live_page(url=c.link, final_url=c.link, text=TITLE + " " + " ".join(reversed(WORDS)))
        for c in candidates
    ]), article=article)
    assert evidence.rewrite.url == candidates[1].link


@pytest.mark.parametrize(("final_url", "expected"), [
    ("", True),
    ("https://paper.example/71001/original-slug", True),
    ("https://www.paper.example/71001/original-slug/?view=reader", True),
    ("https://paper.example/", False),
    ("https://paper.example/unrelated-story", False),
    ("https://paper.example/71001/revised-slug", True),
    ("https://paper.example/71002/different-story", False),
])
async def test_stub_requires_live_headline_words_on_same_page(monkeypatch, final_url, expected):
    article = removed_article().model_copy(update={"content": "Brief update"})
    url = "https://paper.example/71001/original-slug"
    result = await run_similarity(
        monkeypatch, [archive_candidate(link=url, publish_date="2031-03-13")],
        FetchOutcome(live=[live_page(url=url, final_url=final_url, text=TITLE)]), article=article,
    )
    assert result.candidates[0].headline_in_text == 1
    assert result.candidates[0].rewrite is expected


@pytest.mark.parametrize(("text", "share", "expected"), [
    ("Invented council reservoir budget", 0.80, True),
    ("Invented council reservoir", 0.60, False),
    ("Unrelated pottery exhibition", 0, False),
])
async def test_stub_measures_headline_words_in_live_text(monkeypatch, text, share, expected):
    article = removed_article().model_copy(update={"content": "Brief update"})
    result = await run_similarity(
        monkeypatch, [archive_candidate(publish_date="2031-03-13")],
        FetchOutcome(live=[live_page(text=text)]), article=article,
    )
    assert result.candidates[0].headline_in_text == share
    assert result.candidates[0].rewrite is expected


@pytest.mark.parametrize("rewrite_index", [0, 2, 4])
async def test_prompt_keeps_budgeted_pages_and_adds_rewrite_on_each_retry(
    monkeypatch, rewrite_index,
):
    candidate = similarity.SimilarCandidate(
        url=NEW_URL, rewrite=True, title_overlap=0.5, word_overlap=0.6,
        own_words=30, date_gap_days=1,
    )
    others = [live_page(url=f"https://paper.example/unrelated/{i}") for i in range(4)]
    pages = list(others)
    pages.insert(rewrite_index, live_page())
    fetched = FetchOutcome(live=pages)
    prompts = []

    async def agent_run(prompt, model_settings):
        prompts.append(prompt)
        budget, _ = web_search._PROMPT_BUDGETS[len(prompts) - 1]
        expected = pages[:budget]
        if not any(p.url == NEW_URL for p in expected):
            expected.append(pages[rewrite_index])
        assert [line for line in prompt.splitlines() if line.startswith("- ")] == [
            f"- {p.title} ({p.url})" for p in expected
        ]
        assert len(expected) <= budget + 1
        assert NEW_URL in prompt
        if len(prompts) < 3:
            raise ValueError("Invented retry")
        return SimpleNamespace(data=web_search.WebSearchResult(
            matches=[web_search.PageMatch(url=NEW_URL)], reasoning="Invented match",
        ))

    async def gather(*args, **kwargs):
        return [], 0, 0

    async def fetch_new(*args):
        return FetchOutcome()

    monkeypatch.setattr(web_search, "_gather_hits", gather)
    monkeypatch.setattr(web_search, "_fetch_new", fetch_new)
    result = await web_search.run(
        removed_article(), None, SimpleNamespace(run=agent_run), prefetched=fetched,
        rewrites={normalize_url(NEW_URL): candidate},
    )
    assert len(prompts) == 3
    assert fetched.live == pages
    assert result.relocated_to.url == NEW_URL
    assert result.relocation_rewrite.own_words == 30


@pytest.mark.parametrize("reverse", [True, False])
async def test_rewrite_fallback_uses_model_order_and_preserves_missing_facts(monkeypatch, reverse):
    candidates = [archive_candidate(id=i, link=f"{NEW_URL}/{i}") for i in range(2)]
    fetched = FetchOutcome(live=[live_page(url=c.link, final_url=c.link) for c in candidates])
    sim = await run_similarity(monkeypatch, candidates, fetched)
    order = list(reversed(candidates)) if reverse else candidates
    result = web_search._ground(
        web_search.WebSearchResult(matches=[
            web_search.PageMatch(url=c.link, covers_key_facts=False,
                                 omissions=[" ", "Invented budget amount"])
            for c in order
        ], reasoning="Invented rewrites"), fetched, removed_article(), 2,
        rewrites={normalize_url(c.url): c for c in sim.candidates if c.rewrite},
    )
    assert result.relocated_to.url == order[0].link
    assert result.relocation_covers_key_facts is False
    assert result.omissions == ["Invented budget amount"]
    assert [hit.url for hit in result.matches] == [order[0].link]
    assert [hit.url for hit in result.related_same_publisher] == [order[1].link]
    measured = result.same_publisher_measurements[order[0].link]
    assert (result.relocation_old_in_new, result.relocation_new_in_old) == measured.cleaned
    assert (result.raw_old_in_new, result.raw_new_in_old) == measured.raw
    assert result.relocation_rewrite.word_overlap == 1


async def test_selected_partial_copy_keeps_its_rewrite_signal(monkeypatch):
    fetched = FetchOutcome(live=[live_page(text=" ".join(WORDS[:15] + WORDS[15:][::-1]))])
    sim = await run_similarity(monkeypatch, [archive_candidate()], fetched)
    assert sim.candidates[0].fit == "partial"
    assert sim.candidates[0].rewrite is True
    result = web_search._ground(
        web_search.WebSearchResult(matches=[web_search.PageMatch(url=NEW_URL)],
                                   reasoning="Invented partial rewrite"),
        fetched, removed_article(), 1, rewrites={normalize_url(NEW_URL): sim.rewrite},
    )
    assert result.relocated_to.url == NEW_URL
    assert result.relocation_rewrite.word_overlap == sim.rewrite.word_overlap
    assert result.related_same_publisher == []


async def test_measured_republication_takes_precedence_over_rewrite(monkeypatch):
    candidates = [archive_candidate(), archive_candidate(id=2, link=NEW_URL + "/copy")]
    fetched = FetchOutcome(live=[
        live_page(), live_page(url=candidates[1].link, text=" ".join(WORDS)),
    ])
    result = await run_similarity(monkeypatch, candidates, fetched)
    assert result.candidates[0].rewrite is True
    assert result.rewrite is None
    assert result.relocated_to.url == candidates[1].link
