"""Step 1 — classification. The model proposes, the catalog disposes: every notation in the
output must have been SHOWN to the model in that call. TestModel(custom_result_args=…) plays
the model so the grounding is exercised with exact outputs; no socket, no LM Studio."""
import pytest
from pydantic_ai.models.test import TestModel

from src.api.models import Article, GeographicScope
from src.clients.concepts import SCHEME_GENRES, SCHEME_TOPICS, Concept, ConceptCatalog
from src.workflow import classification as cls
from src.workflow.classification import (
    FINE_DEEP_LIMIT, FINE_MAX_NODES, MAX_TAGS, ClassificationAgents, CoarseClassification,
    FineClassification, build_agents, coarse_prompt, fine_menu, fine_prompt, ground_scope,
    ground_tags, ground_top_levels,
)


def topic(notation, label, parent=None, path=None):
    return Concept(notation=notation, scheme=SCHEME_TOPICS, pref_label=label,
                   parent_notation=parent, hierarchy_path=path, definition=f"def of {label}")


TOPICS = [
    topic("medtop:02000000", "crime, law and justice", None, "02000000"),
    topic("medtop:20000082", "crime", "medtop:02000000", "02000000.20000082"),
    topic("medtop:20000093", "corruption", "medtop:20000082", "02000000.20000082.20000093"),
    topic("medtop:20000084", "sexual misconduct", "medtop:20000082", "02000000.20000082.20000084"),
    topic("medtop:20000085", "sex crime", "medtop:20000084", "02000000.20000082.20000084.20000085"),
    topic("medtop:11000000", "politics and government", None, "11000000"),
    topic("medtop:20000593", "government", "medtop:11000000", "11000000.20000593"),
    topic("medtop:20000596", "public officials", "medtop:20000593", "11000000.20000593.20000596"),
    topic("medtop:15000000", "sport", None, "15000000"),
]
GENRES = [Concept(notation="genre:Current", scheme=SCHEME_GENRES, pref_label="Current Events", definition="now"),
          Concept(notation="genre:Opinion", scheme=SCHEME_GENRES, pref_label="Opinion", definition="views")]
ARTICLE = Article(url="https://t24.com.tr/haber/x,1", title="Belediyede rüşvet soruşturması",
                  content="<p>Savcılık, belediye başkanı hakkında rüşvet iddiasıyla soruşturma başlattı.</p>")


@pytest.fixture
def catalog():
    return ConceptCatalog(TOPICS, GENRES)


def agents(coarse: dict, fine: dict) -> ClassificationAgents:
    return build_agents_from(TestModel(custom_result_args=coarse), TestModel(custom_result_args=fine))


def build_agents_from(coarse_model, fine_model) -> ClassificationAgents:
    a = build_agents(coarse_model)
    b = build_agents(fine_model)
    return ClassificationAgents(coarse=a.coarse, fine=b.fine)


GOOD_COARSE = {"top_level_topics": ["medtop:02000000", "medtop:11000000"], "genre": "genre:Current",
               "geographic_scope": "local", "gist": "A prosecutor opened a bribery probe into a mayor."}


# --- grounding (pure) --------------------------------------------------------

def test_top_levels_keep_order_drop_unknown_and_non_top_level(catalog):
    kept, dropped = ground_top_levels(
        ["medtop:11000000", "medtop:20000093", "medtop:99999999", "medtop:02000000", "medtop:11000000"], catalog)
    assert [c.notation for c in kept] == ["medtop:11000000", "medtop:02000000"]
    assert dropped == ["medtop:20000093", "medtop:99999999"]


def test_top_levels_are_capped_at_three(catalog):
    kept, _ = ground_top_levels(["medtop:02000000", "medtop:11000000", "medtop:15000000", "medtop:02000000"], catalog)
    assert len(kept) == 3


def test_scope_accepts_only_story_scopes():
    assert ground_scope("national") is GeographicScope.NATIONAL
    assert ground_scope(" local ") is GeographicScope.LOCAL
    assert ground_scope("platform_specific") is None   # a censorship-reach value, not a story scope
    assert ground_scope("Türkiye") is None
    assert ground_scope("") is None


def test_tags_must_come_from_the_menu_shown(catalog):
    menu = fine_menu([catalog.get("medtop:02000000")], catalog)
    kept, dropped = ground_tags(
        ["medtop:20000093", "medtop:20000596", "corruption", "medtop:20000093", "medtop:02000000"], menu, catalog)
    # 20000596 is a real topic but under politics — NOT shown in this menu — so it is dropped.
    assert [c.notation for c in kept] == ["medtop:20000093", "medtop:02000000"]
    assert dropped == ["medtop:20000596", "corruption"]


def test_tags_are_capped_at_five_in_model_order(catalog):
    menu = fine_menu([catalog.get("medtop:02000000"), catalog.get("medtop:11000000")], catalog)
    raw = ["medtop:20000093", "medtop:20000085", "medtop:20000084", "medtop:20000082",
           "medtop:20000596", "medtop:20000593", "medtop:11000000"]
    kept, _ = ground_tags(raw, menu, catalog)
    assert [c.notation for c in kept] == raw[:MAX_TAGS]


# --- menu budgeting ----------------------------------------------------------

def test_fine_menu_includes_the_top_level_and_its_subtree_to_depth_four(catalog):
    menu = fine_menu([catalog.get("medtop:02000000")], catalog)
    assert [c.notation for c in menu] == ["medtop:02000000", "medtop:20000082", "medtop:20000084",
                                          "medtop:20000085", "medtop:20000093"]


def test_a_huge_subtree_is_listed_to_depth_three_only():
    # Build sport with FINE_DEEP_LIMIT+1 depth-4 nodes under one depth-3 node: economy/sport shape.
    topics = [topic("medtop:15000000", "sport", None, "15000000"),
              topic("medtop:20000822", "competition discipline", "medtop:15000000", "15000000.20000822"),
              topic("medtop:20000823", "football", "medtop:20000822", "15000000.20000822.20000823")]
    topics += [topic(f"medtop:3{i:07d}", f"club {i}", "medtop:20000823", f"15000000.20000822.20000823.3{i:07d}")
               for i in range(FINE_DEEP_LIMIT + 1)]
    cat = ConceptCatalog(topics, GENRES)
    menu = fine_menu([cat.get("medtop:15000000")], cat)
    assert [c.notation for c in menu] == ["medtop:15000000", "medtop:20000822", "medtop:20000823"]


def test_fine_menu_never_exceeds_the_node_cap():
    topics = [topic("medtop:01000000", "arts", None, "01000000")]
    topics += [topic(f"medtop:2{i:07d}", f"art {i}", "medtop:01000000", f"01000000.2{i:07d}") for i in range(FINE_MAX_NODES + 50)]
    topics += [topic("medtop:11000000", "politics", None, "11000000"),
               topic("medtop:20000593", "government", "medtop:11000000", "11000000.20000593")]
    cat = ConceptCatalog(topics, GENRES)
    menu = fine_menu([cat.get("medtop:01000000"), cat.get("medtop:11000000")], cat)
    assert len(menu) <= FINE_MAX_NODES + 1   # +1: a later top-level itself always stays selectable
    assert menu[0].notation == "medtop:01000000"
    assert any(c.notation == "medtop:11000000" for c in menu)


# --- prompts -----------------------------------------------------------------

def test_coarse_prompt_lists_every_top_level_every_genre_and_the_four_scopes(catalog):
    p = coarse_prompt(ARTICLE, "prose", catalog, 1500)
    for c in catalog.top_level():
        assert c.notation in p and c.definition in p
    for g in catalog.genres():
        assert g.notation in p
    for s in ("local", "regional", "national", "global_scope"):
        assert s in p
    assert "platform_specific" not in p
    assert "medtop:20000093" not in p   # depth-3 topics are NOT offered in the coarse call


def test_fine_prompt_shows_the_menu_with_definitions_only_to_depth_two(catalog):
    menu = fine_menu([catalog.get("medtop:02000000")], catalog)
    p = fine_prompt(ARTICLE, "prose", "gist here", menu, 800)
    assert "gist here" in p
    assert "def of crime" in p                  # depth 2: definition shown
    assert "def of corruption" not in p         # depth 3: label only
    assert "medtop:20000093 | corruption" in p


def test_prompts_name_the_result_fields_and_carry_the_guardrail():
    coarse = cls.prompts.CLASSIFICATION_COARSE_PROMPT.lower()
    fine = cls.prompts.CLASSIFICATION_FINE_PROMPT.lower()
    for f in CoarseClassification.model_fields:
        assert f in coarse
    for f in FineClassification.model_fields:
        assert f in fine
    assert "censored" in coarse and "censored" in fine
    assert "todo" not in coarse and "todo" not in fine


# --- run ---------------------------------------------------------------------

async def test_run_returns_grounded_tags_genre_scope_and_gist(catalog):
    out = await cls.run(ARTICLE, agents(GOOD_COARSE, {"topics": ["medtop:20000093", "medtop:20000596", "medtop:bogus"]}), catalog)
    c = out.classification
    assert [t.notation for t in c.subject_tags] == ["medtop:20000093", "medtop:20000596"]
    assert [t.label for t in c.subject_tags] == ["corruption", "public officials"]
    assert c.genre.notation == "genre:Current" and c.genre.label == "Current Events"
    assert c.geographic_scope is GeographicScope.LOCAL
    assert c.gist.startswith("A prosecutor")
    assert out.details["dropped"] == ["medtop:bogus"]
    assert out.details["coarse_top_levels"] == ["medtop:02000000", "medtop:11000000"]


async def test_run_falls_back_to_the_top_levels_when_the_fine_pass_yields_nothing_valid(catalog):
    out = await cls.run(ARTICLE, agents(GOOD_COARSE, {"topics": ["nonsense"]}), catalog)
    assert [t.notation for t in out.classification.subject_tags] == ["medtop:02000000", "medtop:11000000"]


async def test_run_drops_an_unknown_genre_and_an_invalid_scope_but_keeps_the_rest(catalog):
    coarse = dict(GOOD_COARSE, genre="genre:Nope", geographic_scope="platform_specific")
    out = await cls.run(ARTICLE, agents(coarse, {"topics": ["medtop:20000093"]}), catalog)
    assert out.classification.genre is None
    assert out.classification.geographic_scope is None
    assert [t.notation for t in out.classification.subject_tags] == ["medtop:20000093"]
    assert "genre:Nope" in out.details["dropped"] and "platform_specific" in out.details["dropped"]


async def test_no_catalog_means_skipped_not_crashed():
    out = await cls.run(ARTICLE, agents(GOOD_COARSE, {"topics": []}), None)
    assert out.classification is None
    assert out.details.get("skipped") is True
    assert out.explanation.startswith("Skipped")


class Boom:
    def __init__(self):
        self.calls = 0

    async def run(self, prompt, **kwargs):
        self.calls += 1
        raise RuntimeError("empty response")


async def test_a_total_coarse_failure_yields_no_classification_after_two_attempts(catalog):
    boom = Boom()
    out = await cls.run(ARTICLE, ClassificationAgents(coarse=boom, fine=boom), catalog)
    assert out.classification is None
    assert out.details["model_failed"] is True
    assert boom.calls == 2


async def test_a_fine_failure_keeps_the_coarse_result(catalog):
    good = build_agents(TestModel(custom_result_args=GOOD_COARSE))
    out = await cls.run(ARTICLE, ClassificationAgents(coarse=good.coarse, fine=Boom()), catalog)
    assert [t.notation for t in out.classification.subject_tags] == ["medtop:02000000", "medtop:11000000"]
    assert out.details["fine_failed"] is True


def test_build_agents_bind_the_two_prompts():
    a = build_agents(TestModel())
    assert cls.prompts.CLASSIFICATION_COARSE_PROMPT in a.coarse._system_prompts
    assert cls.prompts.CLASSIFICATION_FINE_PROMPT in a.fine._system_prompts


# --- lenient notation parsing (measured 2026-09-09, first live case) ---------------------

def test_a_menu_line_copied_verbatim_still_grounds_to_its_notation(catalog):
    # gemma copied the menu lines back as "medtop:20000093 | corruption" (and once with the
    # indentation); the notation is right there, so the tag must be kept, not dropped.
    menu = fine_menu([catalog.get("medtop:02000000")], catalog)
    kept, dropped = ground_tags(
        ["medtop:20000093 | corruption", "  medtop:20000085 | sex crime", "corruption (medtop:20000084)"],
        menu, catalog)
    assert [c.notation for c in kept] == ["medtop:20000093", "medtop:20000085", "medtop:20000084"]
    assert dropped == []


def test_top_levels_and_genre_accept_the_same_lenient_format(catalog):
    kept, dropped = ground_top_levels(["medtop:02000000 | crime, law and justice | def"], catalog)
    assert [c.notation for c in kept] == ["medtop:02000000"] and dropped == []
    from src.workflow.classification import notation_of
    assert notation_of("genre:Current | Current Events | now") == "genre:Current"
    assert notation_of("Current Events") is None
    assert notation_of("") is None


async def test_run_keeps_lenient_fine_picks_and_a_lenient_genre(catalog):
    coarse = dict(GOOD_COARSE, genre="genre:Current | Current Events")
    out = await cls.run(ARTICLE, agents(coarse, {"topics": ["medtop:20000093 | corruption"]}), catalog)
    assert [t.notation for t in out.classification.subject_tags] == ["medtop:20000093"]
    assert out.classification.genre.notation == "genre:Current"
    assert out.details["dropped"] == []


async def test_each_call_carries_a_small_output_cap(catalog):
    # A correct answer is ~100 tokens; a runaway must end in seconds, not at the provider's 4,096.
    class Recorder:
        def __init__(self, data):
            self.data, self.settings = data, []

        async def run(self, prompt, model_settings=None):
            self.settings.append(model_settings)
            class R:  # noqa: D401 — mimics AgentRunResult
                data = self.data
            return R()

    coarse = Recorder(CoarseClassification(**GOOD_COARSE))
    fine = Recorder(FineClassification(topics=["medtop:20000093"]))
    await cls.run(ARTICLE, ClassificationAgents(coarse=coarse, fine=fine), catalog)
    assert coarse.settings == [{"max_tokens": cls.COARSE_MAX_TOKENS}]
    assert fine.settings == [{"max_tokens": cls.FINE_MAX_TOKENS}]
    assert cls.COARSE_MAX_TOKENS <= 400 and cls.FINE_MAX_TOKENS <= 400


# --- the most-specific-level rule (2026-10-01) -------------------------------------------------

def test_is_ancestor_follows_the_hierarchy_path(catalog):
    from src.workflow.classification import collapse_hierarchy, is_ancestor
    crime, corruption, sexmis, sexcrime = (catalog.get(n) for n in
        ("medtop:20000082", "medtop:20000093", "medtop:20000084", "medtop:20000085"))
    assert is_ancestor(crime, corruption) and is_ancestor(crime, sexcrime) and is_ancestor(sexmis, sexcrime)
    assert not is_ancestor(corruption, crime) and not is_ancestor(corruption, sexmis)
    assert not is_ancestor(crime, crime)
    # root ancestors too
    assert is_ancestor(catalog.get("medtop:02000000"), corruption)
    kept, collapsed = collapse_hierarchy([crime, corruption, sexmis, sexcrime, catalog.get("medtop:20000596")])
    assert [k.pref_label for k in kept] == ["corruption", "sex crime", "public officials"]
    assert collapsed == [("medtop:20000082", "medtop:20000093"), ("medtop:20000084", "medtop:20000085")]


def test_collapse_keeps_order_and_leaves_unrelated_and_sibling_tags_alone(catalog):
    from src.workflow.classification import collapse_hierarchy
    tags = [catalog.get(n) for n in ("medtop:20000093", "medtop:20000084", "medtop:20000596")]
    kept, collapsed = collapse_hierarchy(tags)
    assert [k.notation for k in kept] == ["medtop:20000093", "medtop:20000084", "medtop:20000596"] and collapsed == []


async def test_run_folds_a_parent_into_its_chosen_child_and_reports_it(catalog):
    out = await cls.run(ARTICLE, agents(
        {"top_level_topics": ["medtop:02000000"], "genre": "genre:Current", "geographic_scope": "national", "gist": "g"},
        {"topics": ["medtop:20000082", "medtop:20000093", "medtop:20000084", "medtop:20000085"]}), catalog)
    labels = [t.label for t in out.classification.subject_tags]
    assert labels == ["corruption", "sex crime"]
    assert out.details["collapsed"] == [{"dropped": "medtop:20000082", "covered_by": "medtop:20000093"},
                                        {"dropped": "medtop:20000084", "covered_by": "medtop:20000085"}]
    assert "folded into the narrower pick" in out.explanation


def test_four_or_more_siblings_become_their_parent_at_the_first_siblings_rank(catalog):
    from src.workflow.classification import collapse_siblings
    # four children of "crime" in the fixture would be needed; build them on the fly
    from src.clients.concepts import SCHEME_TOPICS
    kids = [Concept(notation=f"medtop:2000009{i}", scheme=SCHEME_TOPICS, pref_label=f"kid{i}", parent_notation="medtop:20000082",
                    hierarchy_path=f"02000000.20000082.2000009{i}") for i in range(4, 8)]
    cat = ConceptCatalog(TOPICS + kids, GENRES)
    officials = cat.get("medtop:20000596")
    kept, folded = collapse_siblings([kids[0], officials, kids[1], kids[2], kids[3]], cat)
    assert [k.notation for k in kept] == ["medtop:20000082", "medtop:20000596"]
    assert folded == [("medtop:20000082", [k.notation for k in kids])]
    # three siblings are allowed
    kept3, folded3 = collapse_siblings(kids[:3], cat)
    assert len(kept3) == 3 and folded3 == []


def test_a_top_level_topic_is_dropped_only_when_something_more_specific_is_present(catalog):
    from src.workflow.classification import drop_root_fillers
    root, corruption = catalog.get("medtop:11000000"), catalog.get("medtop:20000093")
    kept, dropped = drop_root_fillers([corruption, root])
    assert [k.notation for k in kept] == ["medtop:20000093"] and dropped == ["medtop:11000000"]
    kept2, dropped2 = drop_root_fillers([root])
    assert [k.notation for k in kept2] == ["medtop:11000000"] and dropped2 == []


def test_fine_prompt_carries_the_indexing_rules():
    from src.workflow.prompts import CLASSIFICATION_FINE_PROMPT
    P = " ".join(CLASSIFICATION_FINE_PROMPT.split())          # the prompt is hard-wrapped
    assert "1 to 5 notations" in P and "most articles need 1–3" in P
    assert "never pad" in P and "MOST SPECIFIC" in P and "do NOT also list its broader parent" in P
    assert "nearly the same thing" in P and "at most three topics under the same parent" in P and "human interest" in P
    assert "censored" in P   # the guardrail stays


def test_topics_the_model_rates_below_the_floor_are_left_out(catalog):
    from src.workflow.classification import CONFIDENCE_MIN, drop_low_confidence
    raw = ["medtop:20000093", "medtop:20000596 | public officials", "medtop:20000084"]
    tags = [catalog.get("medtop:20000093"), catalog.get("medtop:20000596"), catalog.get("medtop:20000084")]
    kept, cut = drop_low_confidence(tags, raw, [0.9, 0.4, 0.7])
    assert [k.notation for k in kept] == ["medtop:20000093", "medtop:20000084"]
    assert cut == [("medtop:20000596", 0.4)] and CONFIDENCE_MIN == 0.5
    # no confidences, or a misaligned list → nothing is cut
    assert drop_low_confidence(tags, raw, [])[0] == tags
    assert drop_low_confidence(tags, raw, [0.1])[0] == tags


async def test_run_applies_the_confidence_floor_and_reports_it(catalog):
    out = await cls.run(ARTICLE, agents(
        {"top_level_topics": ["medtop:02000000"], "genre": "genre:Current", "geographic_scope": "national", "gist": "g"},
        {"topics": ["medtop:20000093", "medtop:20000084"], "confidences": [0.95, 0.3]}), catalog)
    assert [t.label for t in out.classification.subject_tags] == ["corruption"]
    assert out.details["low_confidence_dropped"] == [{"notation": "medtop:20000084", "confidence": 0.3}]
    assert "rated below 50%" in out.explanation
