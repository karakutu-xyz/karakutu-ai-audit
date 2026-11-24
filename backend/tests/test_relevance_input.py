"""The relevance step reads article prose, capped, and its prompt keeps the contract."""

from types import SimpleNamespace

import pytest

from audit.domain import Article
from audit.workflow import prompts, relevance
from audit.workflow.prompt_registry import validation_codes


class CapturingAgent:
    def __init__(self):
        self.prompts = []

    async def run(self, user_prompt, model_settings=None):
        self.prompts.append(user_prompt)
        return SimpleNamespace(data=relevance.RelevanceResult(
            is_relevant=True, confidence=0.9, reasoning="public budget"))


PARAGRAPH = (
    "The invented council of a fictional district approved next year's road budget "
    "after a long debate about repairs to the northern bridge and new bus shelters."
)


def article(content):
    return Article(url="https://dusulke.example/haber/1", title="Invented budget vote",
                   content=content)


async def screen(content):
    agent = CapturingAgent()
    await relevance.run(article(content), agent)
    return agent.prompts[0].split("Content:\n", 1)[1]


async def test_archive_html_reaches_the_model_as_prose():
    markup = "<div class='menu'>" + "<a href='/x'>Menu item</a>" * 120 + "</div>"
    html = f"<html><head><script>var x = 1;</script></head><body>{markup}<p>{PARAGRAPH}</p></body></html>"
    assert html.index("<p>") > relevance.MAX_RELEVANCE_CHARS

    content = await screen(html)

    assert content == PARAGRAPH
    assert "<" not in content and "Menu item" not in content


async def test_plain_text_passes_through_unchanged():
    assert await screen(PARAGRAPH) == PARAGRAPH


async def test_the_cap_applies_to_the_prose():
    long_html = "".join(f"<p>{i} {PARAGRAPH}</p>" for i in range(40))
    content = await screen(long_html)
    assert len(content) == relevance.MAX_RELEVANCE_CHARS
    assert content.startswith("0 " + PARAGRAPH)


async def test_markup_without_paragraphs_is_flattened():
    assert await screen("<div>Invented short notice</div>") == "Invented short notice"


async def test_markup_without_any_text_falls_back_to_the_stored_content():
    assert await screen("<br><hr>") == "<br><hr>"


@pytest.mark.parametrize("phrase", [
    "PUBLIC AFFAIRS",
    "they are not required",
    "PRIVATE MATTERS OF INFLUENTIAL PEOPLE ARE OUT OF SCOPE",
    "a court case alone does not make a private",
])
def test_prompt_judges_public_affairs(phrase):
    assert phrase in prompts.RELEVANCE_PROMPT


def test_prompt_keeps_its_contract():
    assert validation_codes("relevance", prompts.RELEVANCE_PROMPT) == []
    assert "GUARDRAIL — this is absolute" in prompts.RELEVANCE_PROMPT
