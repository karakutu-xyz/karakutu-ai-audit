"""src/search/textmatch.py — pure token/shingle containment used to MEASURE relocations.

Measurement replaced judgement here: a model readily takes a same-publisher SIBLING article
(same news cycle, different article) for a republication. These tests pin the
directional-containment semantics the verdict tiers rest on. No I/O, no models.
"""

import pytest

from src.search.textmatch import (
    MOVED_THRESHOLD,
    PARTIAL_MIN,
    SIBLING_MAX,
    containment,
    directional_containments,
    shingles,
    tokens,
)

OLD = (
    "Savcılık, belediye başkanı hakkında ihale sürecinde rüşvet iddiasıyla soruşturma "
    "başlattı. İddiaya göre 12 milyon liralık ihale usulsüz biçimde verildi ve denetim "
    "raporları gizlendi. Tanık ifadelerinde ihale komisyonunun baskı altında karar aldığı "
    "öne sürüldü. Belediye meclisi üyeleri sürecin bağımsız denetçiler tarafından "
    "incelenmesini istedi."
)
DISJOINT = (
    "Şehirde yarın akşam saatlerinde kuvvetli sağanak yağış bekleniyor. Meteoroloji "
    "uyarısına göre rüzgar hızı saatte doksan kilometreye ulaşabilir. Vatandaşların "
    "zorunlu olmadıkça trafiğe çıkmaması önerildi."
)


# --- normalization -----------------------------------------------------------


def test_tokens_are_lowercased_word_chars_of_three_or_more():
    assert tokens("Ali ve 12 milyon TL ödendi") == ["ali", "milyon", "ödendi"]


def test_turkish_and_azeri_letters_survive_normalization():
    # ş, ç, ğ, ə are NOT ascii-folded away. The one deliberate fold is dotless ı → i,
    # a Turkish case-insensitivity repair ("BAŞLATTI" must match "başlattı"): see tokens().
    assert tokens("işçi həbs cəzası") == ["işçi", "həbs", "cəzasi"]
    assert tokens("ağır") == ["ağir"]
    assert tokens("BAŞLATTI") == tokens("başlattı")


def test_dotted_capital_i_folds_to_plain_i():
    # Python's casefold turns İ into "i" + a combining dot; unhandled, "İstanbul" would
    # never match "istanbul" and every Turkish headline word would break the measurement.
    assert tokens("İSTANBUL İhale") == tokens("istanbul ihale")


# --- shingles ----------------------------------------------------------------


def test_shingles_are_k_grams_of_tokens():
    sh = shingles("bir iki dört beş yedi sekiz", k=5)
    assert ("bir", "iki", "dört", "beş", "yedi") in sh
    assert ("iki", "dört", "beş", "yedi", "sekiz") in sh
    assert len(sh) == 2


def test_a_text_shorter_than_k_tokens_still_yields_one_shingle():
    assert len(shingles("aynı üç kelime", k=5)) == 1


def test_an_empty_or_tokenless_text_yields_no_shingles():
    assert shingles("") == frozenset()
    assert shingles("a b c 12 -") == frozenset()


def test_containment_of_an_empty_set_is_zero():
    assert containment(frozenset(), shingles(OLD)) == 0.0


# --- directional containments ------------------------------------------------


def test_verbatim_copy_measures_one_in_both_directions():
    assert directional_containments(OLD, OLD) == (1.0, 1.0)


def test_copy_with_additions_keeps_old_in_new_at_one():
    new = OLD + " Başkanın avukatı iddiaları reddederek sürecin siyasi olduğunu savundu."
    old_in_new, new_in_old = directional_containments(OLD, new)
    assert old_in_new == 1.0
    assert new_in_old < 1.0


def test_shortened_faithful_copy_has_high_new_in_old_and_low_old_in_new():
    new = "Savcılık, belediye başkanı hakkında ihale sürecinde rüşvet iddiasıyla soruşturma başlattı."
    old_in_new, new_in_old = directional_containments(OLD, new)
    assert new_in_old == 1.0
    assert old_in_new < 0.5


def test_disjoint_texts_measure_near_zero_both_ways():
    old_in_new, new_in_old = directional_containments(OLD, DISJOINT)
    assert old_in_new == 0.0
    assert new_in_old == 0.0


@pytest.mark.parametrize("old,new", [("", OLD), (OLD, ""), ("", ""), ("a b 12", OLD)])
def test_unmeasurable_when_either_side_has_no_shingles(old, new):
    assert directional_containments(old, new) == (None, None)


def test_case_differences_do_not_break_a_verbatim_match():
    old_in_new, _ = directional_containments(OLD, OLD.upper())
    assert old_in_new > 0.9


# --- thresholds (calibrated, see the module comment) -------------------------


def test_thresholds_are_ordered_sibling_below_partial_below_moved():
    assert 0.0 < SIBLING_MAX < PARTIAL_MIN < MOVED_THRESHOLD <= 1.0


def test_moved_threshold_is_verbatim_grade():
    # A verbatim republication contains the whole original (old_in_new == 1.0); a rewritten
    # same-event story falls clearly short. The bar must sit above the rewrite.
    assert MOVED_THRESHOLD >= 0.95


# --- distinctive_phrase (stage 3's quoted third search pass) -----------------
#
# A copy can verifiably exist on major outlets while the bare-title search never
# surfaces it — near-identical sibling headlines drown the story. One distinctive sentence
# fragment, searched in quotes, cuts through the title collision. Deterministic, no I/O;
# callers hand in PROSE (fetch.prose_of), like every function in this module.

from src.search.textmatch import (  # noqa: E402
    EARLY_SENTENCES,
    PHRASE_MIN_TOKENS,
    PHRASE_WINDOW,
    distinctive_phrase,
)


def test_phrase_window_sits_in_the_six_to_ten_token_band():
    assert 6 <= PHRASE_WINDOW <= 10
    assert PHRASE_MIN_TOKENS == 6


def test_distinctive_phrase_is_a_verbatim_window_from_the_longest_early_sentence():
    prose = "Kısa bir giriş var. " + OLD
    phrase = distinctive_phrase(prose)

    assert phrase, "a full-length article must yield a phrase"
    assert phrase in prose, "the phrase must be verbatim article text"
    assert phrase not in "Kısa bir giriş var."
    n_tokens = len(phrase.split())
    assert 6 <= n_tokens <= 10


def test_distinctive_phrase_prefers_a_window_with_digits():
    # The first window carries no names or figures; a later one carries "12 milyon".
    sentence = ("belediye başkanı hakkında yürütülen soruşturma kapsamında dün akşam "
                "saatlerinde toplam 12 milyon liralık usulsüz ödeme belgelendi")
    phrase = distinctive_phrase(sentence)

    assert "12" in phrase, "a window carrying digits must be preferred"


def test_distinctive_phrase_prefers_a_proper_noun_but_not_the_sentence_initial_capital():
    # "Belediyenin" is only capitalized because it opens the sentence; "Tepecikova" is a
    # proper noun later on. The window must chase Tepecikova, not settle for position 0.
    sentence = ("Belediyenin su tarifesi konusunda dün yapılan uzun toplantıda Tepecikova "
                "temsilcisi görüş bildirdi")
    phrase = distinctive_phrase(sentence)

    assert "Tepecikova" in phrase
    assert not phrase.startswith("Belediyenin")


def test_distinctive_phrase_falls_back_to_the_first_window_without_anchors():
    sentence = "uzun bir cümle burada hiçbir özel isim veya sayı içermeden devam ediyor"
    phrase = distinctive_phrase(sentence)

    assert phrase.startswith("uzun bir cümle")


def test_distinctive_phrase_only_considers_early_sentences():
    filler = "Bu cümle altı kelimeden biraz uzun sayılır gerçekten. "
    late_long = ("Çok sonra gelen bu upuzun cümle makalenin en uzunu olsa bile hiçbir "
                 "zaman seçilmemeli çünkü erken cümleler arasında değil.")
    prose = filler * EARLY_SENTENCES + late_long
    phrase = distinctive_phrase(prose)

    assert "upuzun" not in phrase
    assert phrase in filler


def test_distinctive_phrase_degrades_to_empty_on_short_or_empty_content():
    assert distinctive_phrase("") == ""
    assert distinctive_phrase("   \n  ") == ""
    assert distinctive_phrase("Sadece dört kelime var.") == ""


def test_distinctive_phrase_strips_embedded_double_quotes():
    sentence = ('Bakan toplantıda "usulsüzlük kanıtlandı ve dosya kapandı" ifadesini '
                "kullanarak konuşmasını bitirdi")
    phrase = distinctive_phrase(sentence)

    assert '"' not in phrase, "the phrase must embed cleanly in a quoted query"


def test_distinctive_phrase_is_deterministic():
    assert distinctive_phrase(OLD) == distinctive_phrase(OLD)
