"""Stage 1 (relevance) — prompt content + structural contract of `run`.

Deliberately NOT asserted here: semantic correctness of the model's verdict.
`TestModel` returns synthetic data, and prompt quality is evaluated out-of-CI.
"""

from pydantic_ai.models.test import TestModel

from src.api.models import Article
from src.workflow import relevance
from src.workflow.relevance import PLACEHOLDER_RELEVANCE_PROMPT, RelevanceResult

ARTICLE = Article(
    url="https://example.com/removed",
    title="Ministry contract awarded without tender",
    content="A ministry awarded a public contract to a firm without a competitive tender.",
)


def _prompt() -> str:
    return PLACEHOLDER_RELEVANCE_PROMPT.lower()


# --- prompt content ----------------------------------------------------------


def test_prompt_has_no_todo_marker():
    assert "todo" not in _prompt()


def test_prompt_names_the_result_fields():
    p = _prompt()
    for field in RelevanceResult.model_fields:
        assert field in p, f"prompt does not mention result field {field!r}"


def test_prompt_lists_in_scope_categories():
    p = _prompt()
    for keyword in ("corruption", "abuse of power", "electoral manipulation"):
        assert keyword in p


def test_prompt_lists_out_of_scope_topic_categories():
    p = _prompt()
    for keyword in ("clickbait", "horoscopes", "lifestyle"):
        assert keyword in p


def test_prompt_lists_the_four_out_of_scope_removal_reasons():
    p = _prompt()
    for keyword in ("retraction", "own work", "migration", "paywalled"):
        assert keyword in p


def test_prompt_contains_censorship_guardrail():
    # The hard editorial rule: this service never infers or emits a censorship claim.
    p = _prompt()
    assert "never infer" in p
    assert "censorship" in p



def test_prompt_states_the_accountability_bar():
    # Editorial Policy: the archive preserves accountability journalism the public
    # had a right to read. Presence of a government actor is not, by itself, scope.
    p = _prompt()
    assert "accountability" in p
    assert "misuse of public" in p
    assert "not in scope merely because a" in p


def test_prompt_excludes_routine_official_coverage():
    # The measured stage-1 false-passes: routine official/administrative coverage
    # must fail even when a government body is the source or the subject.
    p = _prompt()
    assert "routine official coverage is out of scope" in p
    for keyword in (
        "even when made by an official body",
        "ceremonial",
        "awards, openings, seminars, and appointments",
    ):
        assert keyword in p, f"routine-official exclusion is missing {keyword!r}"


def test_prompt_carries_the_measured_false_pass_examples():
    # Inline examples anchor the bar; each passed the old prompt with confidence 1.0.
    p = _prompt()
    for example in (
        "qarlı havaya",        # official statement on school closures for snow
        "emekli aylığı farkı",  # pension payment calendar notice
        "efes-2022",            # minister observes a military exercise (protocol)
        "yenikart",             # award coverage
    ):
        assert example in p, f"false-pass example {example!r} missing from prompt"


def test_prompt_keeps_the_true_positive_examples():
    # Accountability stakes that must keep passing: a protest over violence against
    # workers, a judicial proceeding against an elected official, a harassment
    # investigation of an MP.
    p = _prompt()
    for example in ("sağlık çalışanları", "savunma krizi", "cinsel taciz"):
        assert example in p, f"true-positive example {example!r} missing from prompt"


def test_prompt_pins_the_party_politics_and_diplomacy_boundary():
    # Measured inconsistency in BOTH directions on the 150-case live run: same-genre
    # party statements split pass/fail, and wire diplomacy leaked through. The rule:
    # party/campaign/election/foreign-government coverage is out of scope unless it
    # carries an allegation, rights stakes, or judicial/administrative action.
    p = _prompt()
    assert "party politics and diplomacy" in p
    assert "no allegation of wrongdoing" in p
    assert "foreign government" in p
    assert "routine diplomatic meetings" in p


def test_prompt_carries_the_party_and_diplomacy_contrast_pairs():
    # Each pair anchors one side of the boundary with a live-run headline.
    p = _prompt()
    # party: routine leader statement (out) vs court-ordered takeover / prosecution (in)
    assert "erken seçim çağrısı" in p
    assert "kayyum kararı sonrası" in p
    # diplomacy: wire agreement (out) vs international investigation / sanctions (in)
    assert "adımlarda anlaştık" in p
    assert "sanctions" in p


def test_prompt_defines_confidence_as_certainty_not_p_relevant():
    # Measured: confident rejections emitted confidence 0.0/0.1 — the model read the
    # field as P(relevant). The output contract pins it to certainty in the answer.
    p = _prompt()
    assert "how certain you are of the is_relevant" in p
    assert "a confident no is confidence near 1.0" in p
    assert "never near 0.0" in p


def test_prompt_says_language_and_country_do_not_change_the_bar():
    # Corpus is Turkish + Azerbaijani; the bar is the same in both.
    p = _prompt()
    assert "turkish" in p
    assert "azerbaijani" in p


def test_prompt_output_contract_is_unchanged():
    p = _prompt()
    assert "is_relevant: boolean" in p
    assert "confidence: float between 0.0 and 1.0" in p
    assert "reasoning: string" in p


# --- run() structural contract ----------------------------------------------


async def test_run_returns_relevance_result_from_test_model():
    agent = relevance.build_agent(TestModel())

    result = await relevance.run(ARTICLE, agent)

    assert isinstance(result, RelevanceResult)
    assert isinstance(result.is_relevant, bool)
    assert isinstance(result.confidence, (int, float))
    assert isinstance(result.reasoning, str)


async def test_build_agent_uses_the_relevance_prompt():
    agent = relevance.build_agent(TestModel())

    # 0.0.49: prompt is registered as a system prompt on the agent.
    assert PLACEHOLDER_RELEVANCE_PROMPT in agent._system_prompts


async def test_the_screen_call_carries_a_small_output_cap():
    from src.api.models import Article
    from src.workflow import relevance

    seen = {}

    class Recorder:
        async def run(self, prompt, model_settings=None):
            seen["settings"] = model_settings
            class R:
                data = relevance.RelevanceResult(is_relevant=True, confidence=0.9, reasoning="r")
            return R()

    await relevance.run(Article(url="https://x.example/a", title="t", content="c"), Recorder())
    assert seen["settings"] == {"max_tokens": relevance.RELEVANCE_MAX_TOKENS}
    assert relevance.RELEVANCE_MAX_TOKENS <= 500
