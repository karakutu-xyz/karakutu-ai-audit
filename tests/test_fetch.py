"""src/search/fetch.py — URL normalization, self-exclusion, liveness filtering.

No network: `httpx.AsyncClient` is stubbed via a MockTransport, so every request is
answered in-process.
"""

import httpx
import pytest

from src.search import fetch as fetch_module
from src.search.base import SearchResult
from src.search.fetch import (
    MIN_TEXT_CHARS,
    fetch_candidates,
    guess_language,
    html_to_text,
    is_same_url,
    normalize_url,
    same_article_variant,
    same_publisher,
)


async def fetch_live_pages(hits, exclude_url):
    """Back-compat shim for the tests that only care about the live bucket."""
    return (await fetch_candidates(hits, exclude_url)).live

REMOVED_URL = "https://www.evrensel.net/yazi/98980/washington-da-catlak-sesler"
BODY = "<html><body><p>" + ("Aynı haber metni. " * 40) + "</p></body></html>"
HTML = {"content-type": "text/html; charset=utf-8"}


def hit(url: str, title: str = "T") -> SearchResult:
    return SearchResult(url=url, title=title, snippet="s")


def html_response(status: int = 200, text: str = BODY, **kw) -> httpx.Response:
    """A realistic HTML response. httpx defaults to text/plain, which the fetcher rejects."""
    return httpx.Response(status, text=text, headers=HTML, **kw)


@pytest.fixture
def transport(monkeypatch):
    """Route every fetch through a routing table: url (normalized) -> httpx.Response."""
    routes: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(normalize_url(str(request.url)), httpx.Response(404, text="gone"))

    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", make_client)
    return routes


# --- url normalization -------------------------------------------------------


@pytest.mark.parametrize("a,b", [
    ("https://x.com/a", "http://x.com/a"),            # scheme
    ("https://www.x.com/a", "https://x.com/a"),       # www.
    ("https://x.com/a/", "https://x.com/a"),          # trailing slash
    ("https://X.com/a?utm=1#frag", "https://x.com/a"),  # case, query, fragment
])
def test_urls_that_are_the_same_page(a, b):
    assert is_same_url(a, b)


def test_urls_that_differ_in_path_are_not_the_same():
    # The bug this module exists to prevent: /98980 (removed) vs /98981 (relocated).
    assert not is_same_url(
        "https://www.evrensel.net/yazi/98980/x", "https://www.evrensel.net/yazi/98981/x",
    )


def test_html_to_text_strips_markup_and_scripts():
    para = "<p>" + ("Hello world, this is body copy. " * 3) + "</p>"
    text = html_to_text(para + "<script>evil()</script>")
    assert "Hello world" in text
    assert "evil" not in text


def test_html_to_text_skips_navigation_and_returns_article_prose():
    # Regression: flattening the page and slicing the first N chars fed the model the nav
    # menu ("Künye İletişim Facebook Instagram…") instead of the article, and the model
    # correctly complained it saw "only website navigation elements".
    page = """
    <nav><a href="/x">Künye</a> <a href="/y">İletişim</a> <a href="/z">Facebook</a></nav>
    <div class="menu"><a href="/a">Instagram</a><a href="/b">Spotify</a></div>
    <p>Savcılık, belediye başkanı hakkında ihale sürecinde rüşvet iddiasıyla soruşturma başlattı.</p>
    <p>İddiaya göre 12 milyon liralık ihale usulsüz biçimde verilmiş, denetim raporları gizlenmiştir.</p>
    <footer><p>Tümü</p></footer>
    """
    text = html_to_text(page)

    assert text.startswith("Savcılık")
    assert "rüşvet" in text and "12 milyon" in text
    for nav_item in ("Künye", "Facebook", "Instagram", "Spotify"):
        assert nav_item not in text
    assert "Tümü" not in text  # too short to be prose


def test_html_to_text_decodes_entities():
    para = "<p>" + ("Öcalan&#x27;ın çağrısı &amp; ateşkes kararı açıklandı. " * 2) + "</p>"
    text = html_to_text(para)
    assert "Öcalan'ın" in text
    assert "&" in text and "&#x27;" not in text and "&amp;" not in text


def test_html_to_text_falls_back_when_a_page_has_no_paragraphs():
    text = html_to_text("<div>" + ("Body copy in a div. " * 5) + "</div>")
    assert "Body copy in a div." in text


# --- fetch_live_pages --------------------------------------------------------


async def test_excludes_the_removed_articles_own_url(transport):
    # Even though this URL is alive here, it must never be offered as evidence.
    transport[normalize_url(REMOVED_URL)] = html_response()

    pages = await fetch_live_pages([hit(REMOVED_URL)], exclude_url=REMOVED_URL)

    assert pages == []


async def test_exclusion_ignores_www_and_scheme_differences(transport):
    variant = "http://evrensel.net/yazi/98980/washington-da-catlak-sesler/"
    transport[normalize_url(variant)] = html_response()

    pages = await fetch_live_pages([hit(variant)], exclude_url=REMOVED_URL)

    assert pages == []


async def test_keeps_live_pages_and_drops_dead_ones(transport):
    transport[normalize_url("https://live.com/a")] = html_response()
    # https://dead.com/b is absent from the table -> handler answers 404

    pages = await fetch_live_pages(
        [hit("https://live.com/a"), hit("https://dead.com/b")], exclude_url=REMOVED_URL,
    )

    assert [p.url for p in pages] == ["https://live.com/a"]
    assert pages[0].status == 200
    assert "Aynı haber metni" in pages[0].text


async def test_drops_soft_404_pages_with_too_little_text(transport):
    transport[normalize_url("https://shell.com/a")] = html_response(text="<p>Not found</p>")

    pages = await fetch_live_pages([hit("https://shell.com/a")], exclude_url=REMOVED_URL)

    assert pages == []
    assert len(html_to_text("<p>Not found</p>")) < MIN_TEXT_CHARS


async def test_a_transport_error_drops_the_hit_but_does_not_raise(monkeypatch):
    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        def boom(request):
            raise httpx.ConnectError("no route to host")
        kwargs["transport"] = httpx.MockTransport(boom)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", make_client)

    pages = await fetch_live_pages([hit("https://unreachable.example/a")], exclude_url=REMOVED_URL)

    assert pages == []


async def test_records_the_final_url_after_a_redirect(transport):
    transport[normalize_url("https://old.com/a")] = httpx.Response(
        301, headers={"location": "https://new.com/a"},
    )
    transport[normalize_url("https://new.com/a")] = html_response()

    pages = await fetch_live_pages([hit("https://old.com/a")], exclude_url=REMOVED_URL)

    assert len(pages) == 1
    assert pages[0].url == "https://old.com/a"
    assert pages[0].final_url == "https://new.com/a"


async def test_no_candidates_returns_empty_without_fetching():
    pages = await fetch_live_pages([], exclude_url=REMOVED_URL)
    assert pages == []


async def test_drops_non_html_content_such_as_pdf(transport):
    # Regression: a PDF answers 200, has no <p>, fell through to the flattened-bytes path,
    # cleared the text floor and was served to the model as a readable article — which is
    # how a 2015 municipal budget PDF became "the only retrieved page" for a livestock story.
    transport[normalize_url("https://gov.example/rapor.pdf")] = httpx.Response(
        200, text="%PDF-1.4 " + ("binary noise " * 40),
        headers={"content-type": "application/pdf"},
    )

    pages = await fetch_live_pages([hit("https://gov.example/rapor.pdf")], exclude_url=REMOVED_URL)

    assert pages == []


async def test_html_content_type_is_kept(transport):
    transport[normalize_url("https://news.example/a")] = html_response()

    pages = await fetch_live_pages([hit("https://news.example/a")], exclude_url=REMOVED_URL)

    assert len(pages) == 1


def test_user_agent_is_a_browser_not_a_bot():
    # Regression: evrensel.net answered 403 to a self-identifying bot UA and 200 to a browser
    # UA for the same article, so live relocated copies were being silently discarded.
    from src.search.fetch import _UA

    assert "Mozilla/5.0" in _UA
    assert "karakutu" not in _UA.lower()
    assert "bot" not in _UA.lower()


# --- the dead / unreadable split (this is what keeps a JS shell from looking censored) ---


async def test_an_http_error_is_recorded_as_a_dead_candidate(transport):
    # A 404 is EVIDENCE that a copy existed and is gone. It must survive as such.
    # (https://dead.example/a is absent from the routing table -> handler answers 404)
    out = await fetch_candidates([hit("https://dead.example/a", "Gone")], exclude_url=REMOVED_URL)

    assert out.live == []
    assert [d.url for d in out.dead] == ["https://dead.example/a"]
    assert out.dead[0].status == 404
    assert "404" in out.dead[0].reason


async def test_an_unreachable_host_is_unreadable_not_dead(monkeypatch):
    # We could not reach it. That might mean it is gone — or that the network blinked or the
    # site dropped us. Ambiguous, so it is not evidence.
    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        def boom(request):
            raise httpx.ConnectError("no route to host")
        kwargs["transport"] = httpx.MockTransport(boom)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", make_client)

    out = await fetch_candidates([hit("https://nohost.example/a")], exclude_url=REMOVED_URL)

    assert out.dead == []
    assert [u.url for u in out.unreadable] == ["https://nohost.example/a"]


async def test_a_pdf_is_unreadable_not_dead(transport):
    # A PDF answered 200. It is not a deleted article — it is just not an article.
    # Counting it as "dead" would manufacture censorship evidence out of a search miss.
    transport[normalize_url("https://gov.example/r.pdf")] = httpx.Response(
        200, text="%PDF-1.4", headers={"content-type": "application/pdf"},
    )

    out = await fetch_candidates([hit("https://gov.example/r.pdf")], exclude_url=REMOVED_URL)

    assert out.dead == []
    assert [u.url for u in out.unreadable] == ["https://gov.example/r.pdf"]


async def test_a_js_shell_is_unreadable_not_dead(transport):
    # yenicag.com.cy: 200, but the article is assembled client-side so there is no prose.
    # If this counted as dead, every JS-rendered outlet would look like a censored one.
    transport[normalize_url("https://spa.example/a")] = html_response(text="<p>hi</p>")

    out = await fetch_candidates([hit("https://spa.example/a")], exclude_url=REMOVED_URL)

    assert out.dead == []
    assert len(out.unreadable) == 1


async def test_the_removed_url_is_recorded_as_excluded_not_dead(transport):
    out = await fetch_candidates([hit(REMOVED_URL)], exclude_url=REMOVED_URL)

    assert out.dead == []
    assert out.live == []
    assert out.excluded_self == [REMOVED_URL]


# --- relocation detection is deterministic ----------------------------------


@pytest.mark.parametrize("a,b", [
    ("https://t24.com.tr/gundem/pkk-x,1222402", "https://t24.com.tr/haber/pkk-x,1222402"),
    ("https://www.evrensel.net/yazi/98980/x", "https://evrensel.net/yazi/98981/x"),
    ("https://birgun.net/makale/a", "https://www.birgun.net/haber/b"),
])
def test_same_publisher_recognises_a_relocation(a, b):
    # All 10 relocations in the labelled sample are same-host, different-path.
    assert same_publisher(a, b)


def test_same_publisher_rejects_a_different_outlet():
    # t24 -> birgun is not a relocation; it is another outlet covering the story.
    assert not same_publisher("https://t24.com.tr/haber/a", "https://www.birgun.net/haber/a")


@pytest.mark.parametrize("status", [403, 401, 429, 500, 503])
async def test_a_blocked_or_erroring_page_is_never_dead_evidence(transport, status):
    """403 means WE were blocked, not that the article was deleted.

    Observed live: gazeteduvar.com.tr and evrensel.net both 403 our fetcher while serving
    the article fine to a browser. If that counted as a dead link, any bot-hostile publisher
    would manufacture a POSSIBLE_CENSORSHIP flag out of thin air.
    """
    transport[normalize_url("https://blocked.example/a")] = httpx.Response(status, text="no")

    out = await fetch_candidates([hit("https://blocked.example/a")], exclude_url=REMOVED_URL)

    assert out.dead == [], f"HTTP {status} must not be treated as evidence of deletion"
    assert len(out.unreadable) == 1


@pytest.mark.parametrize("status", [404, 410])
async def test_only_gone_statuses_are_dead_evidence(transport, status):
    # The server explicitly says the resource is not there. This is the only thing that counts.
    transport[normalize_url("https://gone.example/a")] = httpx.Response(status, text="gone")

    out = await fetch_candidates([hit("https://gone.example/a")], exclude_url=REMOVED_URL)

    assert [d.url for d in out.dead] == ["https://gone.example/a"]
    assert out.dead[0].status == status


async def test_a_dead_pdf_is_not_evidence_of_a_deleted_article(transport):
    """A 404 on a PDF is not a deleted news story.

    Observed live: under rate limiting the search backend surfaced
    `emissc.org/files/E-Book/4.+EMI+FULL+TEXT.pdf`, which 404s. Left unchecked it would have
    become "a same-story copy is gone" — a censorship flag resting on a dead academic PDF.
    """
    transport[normalize_url("https://emissc.org/files/E-Book/4.pdf")] = httpx.Response(404)

    out = await fetch_candidates([hit("https://emissc.org/files/E-Book/4.pdf")],
                                 exclude_url=REMOVED_URL)

    assert out.dead == []
    assert len(out.unreadable) == 1


async def test_a_dead_news_url_is_still_evidence(transport):
    transport[normalize_url("https://mlsaturkey.com/tr/mhpli-vekil-uysal")] = httpx.Response(404)

    out = await fetch_candidates([hit("https://mlsaturkey.com/tr/mhpli-vekil-uysal")],
                                 exclude_url=REMOVED_URL)

    assert [d.url for d in out.dead] == ["https://mlsaturkey.com/tr/mhpli-vekil-uysal"]


async def test_a_non_article_url_is_rejected_without_a_network_call(monkeypatch):
    """A .pdf/.jpg/sitemap URL is never a news page, so it must be classed unreadable WITHOUT
    fetching. Regression: downloading five junk PDFs in full took 38.6s; pre-rejecting them
    cut it to ~1s. Here we assert the transport is never even invoked."""
    called = {"n": 0}
    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        def handler(request):
            called["n"] += 1
            return httpx.Response(200, text="x")
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", make_client)

    out = await fetch_candidates([hit("https://x.org/report.pdf")], exclude_url=REMOVED_URL)

    assert called["n"] == 0, "a .pdf URL must not be fetched at all"
    assert len(out.unreadable) == 1
    assert out.dead == [] and out.live == []


# --- same_article_variant: the removed article's own URL in disguise ---------
#
# Measured (2026-08-28/29 run over 150 undecided audits): every false POSSIBLE_CENSORSHIP
# flag rested on a "dead same-story copy" that was really the removed article's OWN URL
# in a different spelling — a junk suffix glued to the slug, or the same numeric article
# id under a new slug. Normalized-URL equality cannot catch those.

_NBSP_REMOVED = ("https://bizim.media/az/cemiyyet/591993/"
                 "qarli-havaya-gore-mektebler-balanacaq-resmi-aiqlama")
_NBSP_VARIANT = _NBSP_REMOVED + "nbsp"


@pytest.mark.parametrize("a,b", [
    # Audits 230409/230408: the archive proposed the removed URL with "nbsp" glued to the
    # slug; it 404s (of course), and was cited as a dead same-story copy.
    (_NBSP_REMOVED, _NBSP_VARIANT),
    # Audit 230237: same numeric article id 78361, different slug — the same article
    # re-slugged by the CMS, not another copy of the story.
    ("https://bizim.media/az/cemiyyet/78361/mudafie-naziri-efes-2022-beynelxalq-telimini-izledi",
     "https://bizim.media/az/cemiyyet/78361/ilahi-edaletdir-kelekle-gelen-kulekle-gedir"),
    # Audits 230076/230078: two slug variants of the same id, each audit citing the other.
    ("https://bizim.media/az/cemiyyet/613098/qezada-olen-ana-ve-ovladlari-son-menzile-yola-salindi",
     "https://bizim.media/az/cemiyyet/613098/qezada-olen-ana-ve-ovladlari"),
    # t24-style comma id: the id names the database row regardless of section or slug.
    ("https://t24.com.tr/gundem/pkk-aciklamasi,1341686",
     "https://t24.com.tr/haber/baska-baslik,1341686"),
    # Junk suffix alone, with no numeric id anywhere in the path.
    ("https://ornek.example/haber/uzun-ve-gercek-bir-haber-slugu",
     "https://ornek.example/haber/uzun-ve-gercek-bir-haber-slugunbsp"),
])
def test_same_article_variant_recognises_the_removed_url_in_disguise(a, b):
    assert same_article_variant(a, b)
    assert same_article_variant(b, a)  # symmetric


@pytest.mark.parametrize("a,b", [
    # Adjacent ids are genuinely different rows — evrensel /yazi/98980 -> /yazi/98981 is a
    # RECORDED RELOCATION and must never be treated as "the same URL".
    ("https://www.evrensel.net/yazi/98980/washington-da-catlak-sesler",
     "https://evrensel.net/yazi/98981/washington-da-catlak-sesler"),
    ("https://www.evrensel.net/haber/597165/", "https://www.evrensel.net/haber/597166/"),
    # Same id on a DIFFERENT host is a coincidence, not a variant.
    ("https://bizim.media/az/cemiyyet/613098/x", "https://oxu.example/az/cemiyyet/613098/x"),
    # A shared year segment in a date path is not an article id.
    ("https://ornek.example/2022/05/bir-haber-basligi",
     "https://ornek.example/2022/06/bambaska-bir-haber"),
    # Too short to be a slug: /tag/a vs /tag/ab is not a junk-suffix variant.
    ("https://ornek.example/tag/a", "https://ornek.example/tag/ab"),
    # Same host, unrelated slugs, no ids.
    ("https://ornek.example/haber/birinci-uzun-haber-slugu",
     "https://ornek.example/haber/ikinci-farkli-haber-slugu"),
])
def test_same_article_variant_is_conservative(a, b):
    assert not same_article_variant(a, b)
    assert not same_article_variant(b, a)


# --- variant exclusion in fetch_candidates -----------------------------------


async def test_a_junk_suffix_variant_is_excluded_without_a_network_call(monkeypatch):
    """Regression (audits 230409/230408): the nbsp variant of the removed URL 404s and was
    cited as a dead same-story copy — a censorship flag built from the removed article's
    own corpse. A junk-suffix variant is textually the removed URL, so it is excluded
    without even fetching."""
    called = {"n": 0}
    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        def handler(request):
            called["n"] += 1
            return httpx.Response(404, text="gone")
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", make_client)

    out = await fetch_candidates([hit(_NBSP_VARIANT)], exclude_url=_NBSP_REMOVED)

    assert called["n"] == 0, "a junk-suffix variant must not be fetched at all"
    assert out.excluded_variant == [_NBSP_VARIANT]
    assert out.dead == [] and out.live == []


async def test_a_dead_reslug_of_the_same_article_id_is_not_dead_evidence(transport):
    # Audit 230237: same id 78361, different slug, HTTP 404 — the removed article itself,
    # not a gone copy of the story. (Absent from the routing table -> the handler 404s.)
    removed = ("https://bizim.media/az/cemiyyet/78361/"
               "mudafie-naziri-efes-2022-beynelxalq-telimini-izledi")
    reslug = "https://bizim.media/az/cemiyyet/78361/ilahi-edaletdir-kelekle-gelen-kulekle-gedir"

    out = await fetch_candidates([hit(reslug)], exclude_url=removed)

    assert out.dead == [], "the removed article's own re-slug must never be dead evidence"
    assert out.excluded_variant == [reslug]


async def test_two_slug_variants_of_one_article_do_not_flag_each_other(transport):
    # Audits 230076/230078: two audits of the same story, EACH citing the other slug
    # variant as its dead copy. Both directions must be excluded.
    a = ("https://bizim.media/az/cemiyyet/613098/"
        "qezada-olen-ana-ve-ovladlari-son-menzile-yola-salindi")
    b = "https://bizim.media/az/cemiyyet/613098/qezada-olen-ana-ve-ovladlari"

    out_ab = await fetch_candidates([hit(b)], exclude_url=a)
    out_ba = await fetch_candidates([hit(a)], exclude_url=b)

    assert out_ab.dead == [] and out_ab.excluded_variant == [b]
    assert out_ba.dead == [] and out_ba.excluded_variant == [a]


async def test_a_live_shared_id_variant_is_still_a_live_page(transport):
    """t24 relocations KEEP the numeric id (/gundem/x,1222402 -> /haber/x,1222402 in the
    labelled sample). A live same-id copy IS the relocation and must stay fetchable —
    variant exclusion applies only to candidates that come back dead."""
    removed = "https://t24.com.tr/gundem/pkk-x,1222402"
    moved = "https://t24.com.tr/haber/pkk-x,1222402"
    transport[normalize_url(moved)] = html_response()

    out = await fetch_candidates([hit(moved)], exclude_url=removed)

    assert [p.url for p in out.live] == [moved]
    assert out.excluded_variant == []


async def test_a_dead_adjacent_id_on_the_same_host_is_still_dead_evidence(transport):
    # Adjacent ids are genuinely different rows: a 404 there is REAL evidence a copy is
    # gone, and must survive the variant exclusion.
    removed = "https://www.evrensel.net/haber/597165/bir-haber-basligi"
    other = "https://www.evrensel.net/haber/597166/baska-bir-haber"

    out = await fetch_candidates([hit(other)], exclude_url=removed)

    assert [d.url for d in out.dead] == [other]
    assert out.excluded_variant == []


async def test_a_dead_candidate_carries_its_hits_source(transport):
    # Stage 3 needs to know a dead candidate came from the archive's similarity index —
    # the archive catalogues REMOVED articles, so its dead links can never be evidence.
    h = SearchResult(url="https://bizim.media/az/cemiyyet/500000/basqa-silinmis-xeber",
                     title="T", snippet="s", source="archive")

    out = await fetch_candidates([h], exclude_url=REMOVED_URL)

    assert [d.source for d in out.dead] == ["archive"]


# --- guess_language ----------------------------------------------------------


@pytest.mark.parametrize("url,lang", [
    ("https://bizim.media/az/cemiyyet/591993/x", "az"),
    ("https://www.bizim.media/cemiyyet/591993/x", "az"),   # the host alone is enough
    ("https://apa.example/az/hadise/123456/x", "az"),      # an /az/ path segment
    ("https://t24.com.tr/gundem/x,1222402", None),
    ("https://www.evrensel.net/yazi/98980/x", None),
    ("https://ornek.example/azerbaycan-haberleri/x", None),  # substring is not a segment
])
def test_guess_language_is_conservative(url, lang):
    assert guess_language(url) == lang


# --- stored prose is for MEASUREMENT; only the prompt is truncated -----------


async def test_stores_full_prose_beyond_the_prompt_cap(transport):
    # Stage 3 measures text containment (relocation vs sibling) on the FULL extracted
    # prose; truncating at storage time would make every long republication measure as a
    # partial fit. Prompt truncation happens later, in web_search._build_prompt.
    long_body = "<p>" + ("Uzun haber metni cümlesi burada devam ediyor. " * 60) + "</p>"
    transport[normalize_url("https://live.com/long")] = html_response(text=long_body)

    pages = await fetch_live_pages([hit("https://live.com/long")], exclude_url=REMOVED_URL)

    assert len(pages) == 1
    assert len(pages[0].text) > 800, "LivePage.text must not be truncated at the prompt cap"


def test_prose_of_extracts_article_prose_from_raw_html():
    # The archive hands removed-article content as RAW HTML; measurement must compare
    # prose with prose, or CTA markup depresses a verbatim copy's containment (measured:
    # 0.40 on audit 230110).
    html = ("<div property='articleBody'><p>"
            + ("Gerçek haber metni burada yer alıyor. " * 3)
            + "</p><div class='evr-sub-cta'>Google Play'den İndir</div></div>")

    text = fetch_module.prose_of(html)

    assert "Gerçek haber metni" in text
    assert "Google Play" not in text


def test_prose_of_leaves_plain_text_alone():
    plain = "Düz metin olduğu gibi kalır; içinde işaretleme yoktur."
    assert fetch_module.prose_of(plain) == plain


# --- redirect-to-self: a fetched page that IS the removed article ------------
#
# Measured (2026-08-30 adversarial QA of the 150-case run): 7 of 8 MOVED verdicts were
# self-citations through a redirect. evrensel 301s ANY slug spelled under an article id to
# the canonical URL — which WAS the removed URL, live. The pipeline fetched the variant,
# followed the redirect, measured containment 1.0 against the removed article ITSELF, and
# called it a relocation. A page whose final URL is the removed URL (or a variant of it)
# is SELF: never a match, never a relocation, never dead evidence.


async def test_a_live_page_redirecting_to_the_removed_url_is_self_not_evidence(transport):
    removed = "https://www.evrensel.net/haber/5971087/ov-der-genel-baskani-aciklama"
    variant = "https://www.evrensel.net/haber/5971087/veli-der-genel-baskani-aciklama"
    transport[normalize_url(variant)] = httpx.Response(301, headers={"location": removed})
    transport[normalize_url(removed)] = html_response()

    out = await fetch_candidates([hit(variant)], exclude_url=removed)

    assert out.live == [], "the removed article itself must never be a live candidate"
    assert out.dead == []
    assert out.excluded_redirect_self == [variant]


async def test_any_url_redirecting_to_the_removed_url_is_excluded_not_just_id_variants(transport):
    # A shortlink or tracking URL with no shared id still lands on the removed article.
    removed = "https://www.evrensel.net/haber/5971087/ov-der-genel-baskani-aciklama"
    shortlink = "https://www.evrensel.net/r/abc"
    transport[normalize_url(shortlink)] = httpx.Response(302, headers={"location": removed})
    transport[normalize_url(removed)] = html_response()

    out = await fetch_candidates([hit(shortlink)], exclude_url=removed)

    assert out.live == []
    assert out.excluded_redirect_self == [shortlink]


async def test_a_redirect_landing_on_a_slug_variant_of_the_removed_url_is_also_self(transport):
    # The final URL shares the removed article's id under another slug — the same row.
    removed = "https://www.evrensel.net/haber/5971087/ov-der-genel-baskani-aciklama"
    requested = "https://www.evrensel.net/amp/5971087"
    landed = "https://www.evrensel.net/haber/5971087/baska-bir-slug-yazimi"
    transport[normalize_url(requested)] = httpx.Response(301, headers={"location": landed})
    transport[normalize_url(landed)] = html_response()

    out = await fetch_candidates([hit(requested)], exclude_url=removed)

    assert out.live == []
    assert out.excluded_redirect_self == [requested]


async def test_a_redirect_to_an_unrelated_article_stays_live(transport):
    # Only landing on SELF excludes; a redirect to a genuinely different page keeps standing.
    transport[normalize_url("https://old.example/a")] = httpx.Response(
        301, headers={"location": "https://new.example/b"},
    )
    transport[normalize_url("https://new.example/b")] = html_response()

    out = await fetch_candidates([hit("https://old.example/a")], exclude_url=REMOVED_URL)

    assert [p.url for p in out.live] == ["https://old.example/a"]
    assert out.excluded_redirect_self == []


async def test_redirect_self_exclusion_is_logged_distinctly(transport, caplog):
    caplog.set_level("INFO", logger="src.search.fetch")
    removed = "https://www.evrensel.net/haber/5971087/ov-der-genel-baskani-aciklama"
    variant = "https://www.evrensel.net/haber/5971087/veli-der-genel-baskani-aciklama"
    transport[normalize_url(variant)] = httpx.Response(301, headers={"location": removed})
    transport[normalize_url(removed)] = html_response()

    await fetch_candidates([hit(variant)], exclude_url=removed)

    assert any("redirect" in r.message and variant in r.message for r in caplog.records)


# --- text/markdown is readable prose -----------------------------------------
#
# Measured (2026-08-30 QA): evrensel served some article pages as `content-type:
# text/markdown`; the fetcher dropped them as "not HTML" — pure recall loss.

_MD_BODY = (
    "# Başlık buraya\n\n"
    + ("Aynı haber metni [kaynak](https://kaynak.example/a) ile devam ediyor. " * 20)
    + "\n\n![foto](https://img.example/f.jpg)\n"
)


async def test_markdown_content_type_is_readable(transport):
    transport[normalize_url("https://www.evrensel.net/md/1")] = httpx.Response(
        200, text=_MD_BODY, headers={"content-type": "text/markdown; charset=utf-8"},
    )

    out = await fetch_candidates([hit("https://www.evrensel.net/md/1")], exclude_url=REMOVED_URL)

    assert len(out.live) == 1
    assert "Aynı haber metni" in out.live[0].text
    assert "kaynak.example" not in out.live[0].text, "link targets must be stripped"


def test_markdown_to_text_strips_syntax_and_link_targets():
    md = (
        "## Alt başlık\n\n"
        "> Alıntı satırı burada uzun bir cümle olarak devam ediyor elbette.\n\n"
        "Bu **kalın** ve *eğik* bir [bağlantı](https://x.example/yol) içeren, `kod` ve "
        "https://cikarilacak.example/adres gibi çıplak adresler taşıyan bir paragraftır.\n\n"
        "![resim açıklaması](https://img.example/a.png)\n\n"
        "[ref]: https://ref.example/hedef\n"
    )

    text = fetch_module.markdown_to_text(md)

    assert "kalın" in text and "eğik" in text and "bağlantı" in text
    for gone in ("https://", "x.example", "cikarilacak.example", "ref.example",
                 "**", "##", "](", "`"):
        assert gone not in text, f"{gone!r} must be stripped from markdown prose"


async def test_json_content_type_is_still_unreadable(transport):
    transport[normalize_url("https://api.example/a")] = httpx.Response(
        200, text='{"story": "' + "uzun metin " * 40 + '"}',
        headers={"content-type": "application/json"},
    )

    out = await fetch_candidates([hit("https://api.example/a")], exclude_url=REMOVED_URL)

    assert out.live == []
    assert len(out.unreadable) == 1


# --- every kept live page gets one INFO log line ------------------------------
#
# Live-but-unmatched pages never used to appear in the per-URL fetch logs (only dead and
# unreadable did), so downstream triage tooling could not surface them as human leads.


async def test_each_kept_live_page_gets_one_info_log_line(transport, caplog):
    caplog.set_level("INFO", logger="src.search.fetch")
    transport[normalize_url("https://live.com/a")] = html_response()

    await fetch_candidates([hit("https://live.com/a")], exclude_url=REMOVED_URL)

    lines = [r.message for r in caplog.records
             if "https://live.com/a" in r.message and "live" in r.message.lower()
             and "chars of prose" in r.message]
    assert len(lines) == 1


async def test_the_live_log_line_marks_a_differing_final_url(transport, caplog):
    caplog.set_level("INFO", logger="src.search.fetch")
    transport[normalize_url("https://old.example/a")] = httpx.Response(
        301, headers={"location": "https://new.example/b"},
    )
    transport[normalize_url("https://new.example/b")] = html_response()

    await fetch_candidates([hit("https://old.example/a")], exclude_url=REMOVED_URL)

    line = next(r.message for r in caplog.records
                if "https://old.example/a" in r.message and "chars of prose" in r.message)
    assert "https://new.example/b" in line, "a redirected live page must name its final URL"


# --- probe_removed_url: the audit-premise check ------------------------------
#
# Measured (2026-08-30 QA): all 8 sampled "removed" URLs answered 200 — the pipeline had
# no way to notice the article was simply live at its original URL.


async def test_probe_removed_url_returns_the_live_page_when_it_answers(transport):
    transport[normalize_url(REMOVED_URL)] = html_response()

    page = await fetch_module.probe_removed_url(REMOVED_URL)

    assert page is not None
    assert page.status == 200
    assert "Aynı haber metni" in page.text


async def test_probe_removed_url_returns_none_when_the_url_is_dead(transport):
    # Absent from the routing table -> the handler answers 404.
    assert await fetch_module.probe_removed_url(REMOVED_URL) is None


async def test_probe_removed_url_never_raises_on_an_unreachable_host(monkeypatch):
    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        def boom(request):
            raise httpx.ConnectError("no route to host")
        kwargs["transport"] = httpx.MockTransport(boom)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", make_client)

    assert await fetch_module.probe_removed_url("https://nohost.example/a") is None


async def test_probe_removed_url_returns_none_for_a_soft_404_shell(transport):
    transport[normalize_url(REMOVED_URL)] = html_response(text="<p>Bulunamadı</p>")

    assert await fetch_module.probe_removed_url(REMOVED_URL) is None
