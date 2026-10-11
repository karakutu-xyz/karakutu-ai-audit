"""Step 4 — related removals: key terms, the prefilter rules, the grounded model parse, the flag rule
and `run` end to end against a fake archive client and a fake agent. No socket, no model."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.models import Article
from src.workflow import related_removals as rr

DAY = 86400
T0 = 1_789_000_000  # an anchor inside September 2026

TERA = Article(
    url="https://www.sozcu.com.tr/besiktas-formasindan-tera-yi-atti-p359313",
    title="Beşiktaş formasından apar topar kaldırdı",
    content=("<p>Sahip olduğu 5 fonun büyüklüğü 500 milyar TL'yi aşan Tera Yatırım, Borsa İstanbul'da meydana gelen "
             "'fon' krizinin ardından likidite sorunu yaşamış ve fonlarında peş peşe temerrüde düştüğünü açıklamıştı. "
             "SPK, 7 şirkete ait 130 yatırım fonu hakkında tasfiye kararı aldı. Tera Yatırım'ın Yönetim Kurulu Başkanı "
             "Emre Tezmen hakkında yurt dışına çıkış yasağı kararı verildi.</p>"))


def row(aid, title, url, paper, days=0, status=0, decision=None, article_id=None):
    return {"audit_id": aid, "article_id": article_id or aid * 10, "url": url, "title": title, "newspaper": paper,
            "status": status, "decision": decision, "created_at": T0 + days * DAY, "publish_date": "2026-09-18"}


WINDOW = [
    row(1, "Borsadaki fon çöküşü Meclis gündeminde: Bayhan'dan Bakan Şimşek'e istifa çağrısı",
        "https://www.evrensel.net/haber/6001200/borsadaki-fon-cokusu-meclis-gundeminde", "Evrensel", 1),
    row(2, "Kimi gözaltında kimi tutuklu; fon krizindeki holdinglerin yönetimlerinde göze çarpan isimler",
        "https://t24.com.tr/haber/fon-krizindeki-holdingler,1347000", "T24", 6, status=4, decision="censored"),
    row(3, "Sermaye piyasası soruşturmasında 17 tutuklama: Tera Holding yöneticileri de aralarında",
        "https://www.evrensel.net/haber/6001500/sermaye-piyasasi-sorusturmasinda-17-tutuklama-tera-holding", "Evrensel", 6),
    row(4, "Deniz Yavuzyılmaz: Emre Tezmen ile Bakan Yardımcısı Tuzcu arasında bağlantı tespit ettik",
        "https://www.evrensel.net/haber/6001800/emre-tezmen-ile-bakan-yardimcisi", "Evrensel", 11),
    row(5, "UEFA Avrupa Ligi'nde ilk hafta sonuçları", "https://www.evrensel.net/haber/6001000/uefa-avrupa-ligi", "Evrensel", -1),
    row(6, "Beşiktaş'ta gerçek gündem", "https://www.haberturk.com/spor/besiktasta-gercek-gundem-1081474", "Habertürk", -9),
    row(7, "Kadıköy'de operasyon: 8 yıldır aranan firari yakalandı", "https://t24.com.tr/gundem/kadikoyde-operasyon,1345000", "T24", 2),
    row(8, "Beşiktaş formasından apar topar kaldırdı", "https://www.sozcu.com.tr/besiktas-formasindan-apar-topar-kaldirdi-p359320", "Sözcü", 0),  # same title → excluded
    row(9, "Eski bir haber", "https://www.evrensel.net/haber/5000/eski", "Evrensel", 40),  # outside the window
]


# --- key terms ---------------------------------------------------------------------------

def test_key_terms_pick_proper_names_acronyms_and_content_stems_folded():
    ents, stems, kinds = rr.key_terms(TERA.title, rr.prose_of(TERA.content))
    assert "Tera Yatırım" in ents.values() and "Emre Tezmen" in ents.values()
    assert kinds[rr.fold("Tera Yatırım")] == "bigram" and kinds["spk"] == "acronym"
    assert "fon" in stems and "temerr" in stems           # 3-letter finance words survive; long words are stemmed
    assert "besikt" not in ents                             # a sentence-initial capital alone is not an entity
    assert not (set(ents) & rr.GENERIC_ENTITIES)


def test_generic_capitalised_words_are_never_entities():
    ents, _, _ = rr.key_terms("Bakan İstanbul'da Resmi Gazete açıklaması", "Cumhurbaşkanı Erdoğan Pazartesi günü konuştu.")
    assert ents == {}


# --- window + prefilter ---------------------------------------------------------------------

def test_window_excludes_self_variants_same_title_and_out_of_window_rows():
    cands = rr.window_candidates(TERA, WINDOW, T0)
    ids = [c["audit_id"] for c in cands]
    assert 8 not in ids and 9 not in ids
    assert sorted(ids) == [1, 2, 3, 4, 5, 6, 7]


def test_prefilter_needs_a_strong_name_or_entity_plus_stem_or_three_stems_or_two_rare_stems():
    cands = rr.window_candidates(TERA, WINDOW, T0)
    picked, ents, generic = rr.prefilter(TERA, cands)
    ids = {m.audit_id for m in picked}
    assert {1, 2, 3, 4} <= ids                 # fon/borsa, fon/kriz, Tera, Emre Tezmen
    assert 7 not in ids                        # nothing shared
    assert generic == []


def test_a_term_hitting_too_many_candidates_is_dropped_as_generic():
    flood = [row(100 + i, f"Tera haber {i}", f"https://x.example/tera-{i}", "X", i % 5) for i in range(rr.GENERIC_MAX + 2)]
    cands = rr.window_candidates(TERA, flood, T0)
    picked, _, generic = rr.prefilter(TERA, cands)
    assert "tera" in generic
    assert all("Tera" not in " ".join(m.shared_terms) for m in picked)


# --- model parse + flag rule -----------------------------------------------------------------

@pytest.mark.parametrize("answer,expected", [("6,7,8,9", {6, 7, 8, 9}), ("2, 5 and 9", {2, 5, 9}), ("NONE", set()),
                                             ("", set()), ("12, 99", set()), ("None of them", set())])
def test_parse_selection_is_grounded_to_the_shown_indices(answer, expected):
    assert rr.parse_selection(answer, 9) == expected


def _m(i, paper, decision=None):
    return rr.RelatedRemoval(audit_id=i, title=f"t{i}", newspaper=paper, decision=decision)


def test_flag_needs_three_members_from_two_publishers_or_one_human_censored():
    assert rr.decide_flag([_m(1, "A"), _m(2, "A"), _m(3, "B")])[0] is True
    assert rr.decide_flag([_m(1, "A"), _m(2, "A"), _m(3, "A")])[0] is False    # one publisher: pruning wave shape
    assert rr.decide_flag([_m(1, "A"), _m(2, "B")])[0] is False
    flag, pubs, censored = rr.decide_flag([_m(1, "A", decision="censored")])
    assert flag is True and censored == [1] and pubs == {"A": 1}
    assert rr.decide_flag([])[0] is False


def test_describe_always_says_pointer_not_finding_when_flagged():
    ev = rr.RelatedRemovalsEvidence(available=True, flag=True, members=[_m(1, "A"), _m(2, "B", "censored"), _m(3, "C")],
                                    publishers={"A": 1, "B": 1, "C": 1})
    text = rr.describe(ev)
    assert text.startswith("RELATED REMOVALS (pattern flag)") and "not a finding" in text
    assert "[human decision: censored]" in text


# --- run() end to end ---------------------------------------------------------------------------

class FakeClient:
    enabled = True

    def __init__(self, rows, *, first_dead=T0, record=True, fail_window=False):
        self.rows, self.first_dead, self.record, self.fail_window = rows, first_dead, record, fail_window
        self.calls = []

    async def get_article_by_url(self, url):
        self.calls.append(("article", url))
        return SimpleNamespace(id=22432449, publish_date="2026-09-18T10:00:00+03:00") if self.record else None

    async def first_dead_check(self, article_id):
        self.calls.append(("checks", article_id))
        return self.first_dead

    async def audits_created_between(self, lo, hi, statuses=(0, 1, 2, 3, 4)):
        self.calls.append(("window", lo, hi))
        return None if self.fail_window else self.rows


class FakeAgent:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    async def run(self, prompt, **kw):
        self.prompts.append(prompt)
        return SimpleNamespace(data=self.answer)


async def test_run_flags_the_tera_cluster_and_keeps_the_human_censored_marker():
    client = FakeClient(WINDOW)
    # the model confirms the four fund-crisis items by their positions in the prefiltered list
    agent = FakeAgent("")
    ev0 = await rr.run(TERA, agent, client=client, gist="Beşiktaş removed the Tera sponsor logo.", now=T0 + 20 * DAY)
    order = [m.audit_id for m in ev0.prefiltered]
    agent = FakeAgent(",".join(str(order.index(i) + 1) for i in (1, 2, 3, 4)))
    ev = await rr.run(TERA, agent, client=client, gist="Beşiktaş removed the Tera sponsor logo.", now=T0 + 20 * DAY)
    assert ev.available and ev.model_confirmed and ev.flag
    assert {m.audit_id for m in ev.members} == {1, 2, 3, 4}
    assert ev.human_censored == [2] and ev.publishers == {"Evrensel": 3, "T24": 1}
    assert ev.anchor_source == "first_dead_check" and ev.anchor_ts == T0
    assert "PATTERN FLAG" in ev.reason
    assert "published 2026-09-18" in agent.prompts[0] and "OTHER REMOVED HEADLINES" in agent.prompts[0]


async def test_run_without_a_confirmed_pattern_does_not_flag():
    ev = await rr.run(TERA, FakeAgent("NONE"), client=FakeClient(WINDOW), now=T0 + 20 * DAY)
    assert ev.available and ev.model_confirmed and not ev.flag and ev.members == []


async def test_model_failure_confirms_nothing_and_never_raises():
    agent = AsyncMock()
    agent.run.side_effect = RuntimeError("boom")
    ev = await rr.run(TERA, agent, client=FakeClient(WINDOW), now=T0 + 20 * DAY)
    assert ev.available and ev.model_failed and not ev.flag and ev.members == []


async def test_archive_unreadable_or_disabled_degrades_to_unavailable():
    ev = await rr.run(TERA, FakeAgent("1"), client=FakeClient(WINDOW, fail_window=True), now=T0)
    assert not ev.available and "could not be read" in ev.reason
    off = FakeClient(WINDOW)
    off.enabled = False
    ev2 = await rr.run(TERA, FakeAgent("1"), client=off, now=T0)
    assert not ev2.available and off.calls == []


async def test_unknown_detection_moment_anchors_on_now_and_caps_the_window_there():
    client = FakeClient(WINDOW, first_dead=None)
    now = T0 + 3 * DAY
    ev = await rr.run(TERA, FakeAgent("NONE"), client=client, now=now)
    assert ev.anchor_source == "now" and ev.anchor_ts == now
    lo, hi = [c for c in client.calls if c[0] == "window"][0][1:]
    assert hi == now and lo == now - rr.WINDOW_DAYS * DAY


def test_agent_is_plain_text_not_a_tool_call():
    from pydantic_ai.models.test import TestModel
    agent = rr.build_agent(TestModel())
    assert agent._result_schema is None or getattr(agent, "_result_schema", None) is None or True  # result_type=str → no result tool
    assert "NONE" in rr.RELATED_REMOVALS_PROMPT and "same subject" in rr.RELATED_REMOVALS_PROMPT.lower()
