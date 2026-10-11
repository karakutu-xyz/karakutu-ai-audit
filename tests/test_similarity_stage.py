"""Step 2 — find similar link. The archive proposes candidates; the fetch gate and the
containment measurement decide what they are. No model. `fetch_candidates` is stubbed."""
import pytest

from src.api.models import Article
from src.clients.models import SimilarArticle
from src.search.fetch import DeadCandidate, FetchOutcome, LivePage, UnreadableCandidate
from src.workflow import similarity

REMOVED_TEXT = " ".join(f"kelime{i} sözcük{i} olay{i}" for i in range(40))
REMOVED = Article(url="https://evrensel.net/yazi/98980/eski", title="Başlık",
                  content=f"<p>{REMOVED_TEXT}</p>")


class FakeClient:
    def __init__(self, similar, enabled=True):
        self.enabled, self._similar, self.calls = enabled, similar, []

    async def find_similar_by_url(self, url, limit=5, date_range_days=30):
        self.calls.append((url, limit))
        return self._similar


def sim(link, score=0.9, title="t"):
    return SimilarArticle(id=1, link=link, title=title, similarity_score=score)


def live(url, text):
    return LivePage(url=url, final_url=url, status=200, title="t", text=text)


@pytest.fixture
def fetched(monkeypatch):
    state = {"outcome": FetchOutcome(), "hits": None, "exclude_url": None}

    async def fake(hits, exclude_url):
        state["hits"], state["exclude_url"] = hits, exclude_url
        return state["outcome"]

    monkeypatch.setattr(similarity, "fetch_candidates", fake)
    return state


async def test_disabled_client_makes_no_lookup(fetched):
    ev = await similarity.run(REMOVED, client=FakeClient([], enabled=False))
    assert ev.available is False and ev.candidates == [] and ev.hits == []
    assert fetched["hits"] is None


async def test_no_candidates_is_unavailable_with_a_reason(fetched):
    ev = await similarity.run(REMOVED, client=FakeClient([]))
    assert ev.available is False and "no similar articles" in ev.reason


async def test_candidates_go_through_the_fetch_gate_excluding_the_removed_url(fetched):
    client = FakeClient([sim("https://evrensel.net/yazi/98981/yeni"), sim("https://birgun.net/haber/x")])
    await similarity.run(REMOVED, client=client)
    assert client.calls == [(REMOVED.url, similarity.ARCHIVE_CANDIDATES)]
    assert [h.url for h in fetched["hits"]] == ["https://evrensel.net/yazi/98981/yeni", "https://birgun.net/haber/x"]
    assert all(h.source == "archive" for h in fetched["hits"])
    assert fetched["exclude_url"] == REMOVED.url


async def test_a_verbatim_same_publisher_copy_is_a_measured_relocation(fetched):
    url = "https://evrensel.net/yazi/98981/yeni"
    fetched["outcome"] = FetchOutcome(live=[live(url, REMOVED_TEXT + " ek bir cümle daha")])
    ev = await similarity.run(REMOVED, client=FakeClient([sim(url)]))
    assert ev.available is True
    assert ev.relocated_to.url == url and ev.relocated_to.source == "archive"
    assert ev.relocation_old_in_new >= similarity.MOVED_THRESHOLD
    c = ev.candidates[0]
    assert c.status == "live" and c.same_publisher and c.fit == "verbatim"
    assert "measured republication" in ev.reason


async def test_a_partial_same_publisher_fit_is_recorded_but_not_decided(fetched):
    url = "https://evrensel.net/yazi/98981/yeni"
    half = " ".join(REMOVED_TEXT.split()[:60]) + " bambaşka sözler " * 30
    fetched["outcome"] = FetchOutcome(live=[live(url, half)])
    ev = await similarity.run(REMOVED, client=FakeClient([sim(url)]))
    assert ev.relocated_to is None
    c = ev.candidates[0]
    assert c.status == "live" and c.fit == "partial"
    assert 0 < c.old_in_new < similarity.MOVED_THRESHOLD


async def test_a_sibling_is_labelled_sibling(fetched):
    url = "https://evrensel.net/haber/5991608/baska"
    fetched["outcome"] = FetchOutcome(live=[live(url, "tamamen farkli bir metin " * 40)])
    ev = await similarity.run(REMOVED, client=FakeClient([sim(url)]))
    assert ev.relocated_to is None and ev.candidates[0].fit == "sibling"


async def test_another_outlet_is_never_measured_here(fetched):
    url = "https://birgun.net/haber/x"
    fetched["outcome"] = FetchOutcome(live=[live(url, REMOVED_TEXT)])
    ev = await similarity.run(REMOVED, client=FakeClient([sim(url)]))
    c = ev.candidates[0]
    assert c.status == "live" and not c.same_publisher and c.old_in_new is None and c.fit is None
    assert ev.relocated_to is None


async def test_dead_unreadable_and_excluded_statuses_are_reported(fetched):
    dead_url, unread_url, self_url = ("https://birgun.net/haber/gone", "https://x.net/a.pdf",
                                      "https://evrensel.net/yazi/98980/eski-kopya")
    fetched["outcome"] = FetchOutcome(
        dead=[DeadCandidate(url=dead_url, title="t", snippet="s", status=404, reason="HTTP 404", source="archive")],
        unreadable=[UnreadableCandidate(url=unread_url, title="t", reason="not HTML")],
        excluded_variant=[self_url],
    )
    ev = await similarity.run(REMOVED, client=FakeClient([sim(dead_url), sim(unread_url), sim(self_url)]))
    assert [c.status for c in ev.candidates] == ["dead", "unreadable", "excluded_variant"]
    assert ev.fetched == fetched["outcome"]


async def test_the_best_verbatim_copy_wins_when_several_measure(fetched):
    a, b = "https://evrensel.net/yazi/1/a", "https://evrensel.net/yazi/2/b"
    fetched["outcome"] = FetchOutcome(live=[live(a, REMOVED_TEXT + " x " * 200), live(b, REMOVED_TEXT)])
    ev = await similarity.run(REMOVED, client=FakeClient([sim(a, 0.5), sim(b, 0.9)]))
    assert ev.relocated_to.url in (a, b)
    assert ev.relocation_old_in_new >= similarity.MOVED_THRESHOLD
