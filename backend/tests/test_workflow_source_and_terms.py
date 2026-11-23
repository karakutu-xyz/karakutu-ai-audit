"""Same-story bar for a cited source, and the related-removals key terms. Invented text only."""
from types import SimpleNamespace

import pytest

from audit.domain import Article
from audit.search.fetch import FetchOutcome, LivePage
from audit.workflow import related_removals as rr
from audit.workflow import source

CITED = "https://agency.example/gundem/2026/01/02/fictional-harbour-crane-report-12345"
STORY = (
    "Liman işletmesi Kuzeyhan'da yeni vinçlerin ihalesini iptal etti, belediye meclisi "
    "kararı oybirliğiyle aldı ve gerekçeyi bir sonraki oturumda açıklayacağını duyurdu. "
    "İşçiler vardiya düzeninin değişeceğini, sendika temsilcileri ise görüşmelerin "
    "sürdüğünü söyledi. Kararın ardından liman çevresinde trafik düzenlemesi yapıldı ve "
    "yetkililer yeni takvimin önümüzdeki ay ilan edileceğini bildirdi. "
) * 2
OTHER = (
    "Kuzeyhan kültür festivali bu yıl üç gün sürecek, konserler sahil parkında yapılacak "
    "ve bilet satışları gelecek hafta başlayacak. Organizasyon komitesi gönüllü başvurularını "
    "topladığını, çocuk atölyelerinin de programda yer alacağını açıkladı. "
) * 2


def removed() -> Article:
    return Article(url="https://news.example/ekonomi/fictional-harbour-cranes-777",
                   title="Kuzeyhan limanında vinç ihalesi iptal edildi",
                   content=f'<p>{STORY}</p><p>Kaynak: <a href="{CITED}">Kuzey Ajans</a></p>')


async def audit_with_cited_page(monkeypatch, page_text: str) -> source.SourceEvidence:
    async def no_self(url, title):
        return None

    async def fetch(hits, exclude_url):
        return FetchOutcome(live=[LivePage(url=CITED, final_url=CITED, status=200,
                                           title="cited", text=page_text)])

    class Agent:
        async def run(self, prompt, model_settings=None):
            return SimpleNamespace(data=source.SourceResult(
                source_kind="agency", source_name="Kuzey Ajans", source_url=CITED))

    monkeypatch.setattr(source, "probe_removed_url", no_self)
    monkeypatch.setattr(source, "fetch_candidates", fetch)
    return await source.run(removed(), Agent(), search_provider=None)


async def test_a_cited_page_carrying_the_story_is_the_same_story_and_a_lead(monkeypatch):
    ev = await audit_with_cited_page(monkeypatch, STORY)
    assert ev.source_status == "live"
    assert ev.source_containment >= source.CITED_SAME_STORY_MIN
    assert ev.live_source is not None and ev.live_source.url == CITED
    assert "LIVE and the same story" in ev.reasoning


@pytest.mark.parametrize("page_text", [OTHER, "kısa"], ids=["other-story", "unmeasurable"])
async def test_a_cited_page_below_the_bar_is_not_the_same_story(monkeypatch, page_text):
    # A different story from the same outlet, or a page too short to measure.
    ev = await audit_with_cited_page(monkeypatch, page_text)
    assert ev.source_status == "live_other"
    assert ev.live_source is None            # never handed to web search as a lead
    assert ev.dead_source is None
    assert "not shown to be the same story" in ev.reasoning
    assert f"{source.CITED_SAME_STORY_MIN:.0%} same-story bar" in ev.reasoning
    assert "LIVE" not in ev.reasoning.split("The text credits")[1]


def test_the_same_story_bar_is_the_calibrated_sibling_line():
    from audit.search.textmatch import SIBLING_MAX
    assert source.CITED_SAME_STORY_MIN == SIBLING_MAX


def kinds_of(title: str, text: str = "") -> dict:
    _, _, kinds = rr.key_terms(title, text)
    return kinds


def test_an_isolated_acronym_is_a_strong_term():
    assert kinds_of("Kuzeyhan'da KZB soruşturması genişledi")["kzb"] == "acronym"


@pytest.mark.parametrize("title", [
    "SON DAKİKA: KZB LİMAN İŞÇİLERİ GREVDE",  # an all-caps headline run
    "ÖZEL HABER Kuzeyhan limanında yeni karar",
    "FLAŞ Kuzeyhan limanında yeni karar",       # a lone header label
])
def test_header_runs_and_labels_are_not_acronyms(title):
    assert "acronym" not in kinds_of(title).values()


@pytest.mark.parametrize("pair", ["Anonim Şirketi", "Yatırım Holding", "Liman Holding",
                                  "Deniz Sanayi", "Kuzey Group"])
def test_company_and_legal_form_pairs_are_not_strong_bigrams(pair):
    kinds = kinds_of(f"Dava açıldı: Kuzeyhan {pair} yöneticisi ifade verdi")
    assert not [k for k, v in kinds.items() if v == "bigram" and k.split()[-1] in
                rr.LEGAL_FORM_PARTS | {rr.fold(pair.split()[-1])}]


def test_a_named_pair_is_still_a_strong_bigram():
    kinds = kinds_of("Soruşturmada Selin Karaduman ifade verdi")
    assert kinds["selin karaduman"] == "bigram"


@pytest.mark.parametrize("ctext, expected", [
    ("selin karaduman ifade verdi", True),
    ("selin karadumanin ifadesi", True),       # a short Turkish suffix on the last word
    ("eselin karaduman ifade verdi", False),   # not from a word start
    ("selin karadumanlarinizdan", False),      # more than a short suffix
    ("selin   karaduman", True),
])
def test_bigrams_match_on_word_boundaries(ctext, expected):
    assert rr.term_matches("selin karaduman", ctext) is expected
