"""Stage 1 — public-interest screening of the removed article."""

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

from src.api.models import Article
from src.workflow import prompts

# Load-bearing name (the `PLACEHOLDER_` prefix is a documented misnomer, kept for tests
# and AGENTS.md). The text lives in `prompts.py`.
PLACEHOLDER_RELEVANCE_PROMPT = prompts.RELEVANCE_PROMPT

# Cap the article text fed to the screen. `Article.content` is caller-supplied and unbounded;
# a full article made the relevance call take 236s (vs the usual ~20s) and, with arbitrary
# input, is a denial-of-service surface. Stage 1 only judges public-interest SCOPE — the
# title plus a generous lede is plenty; it never needs the whole body.
MAX_RELEVANCE_CHARS = 2000
# Output cap per call (merged over the provider default). A screening answer is ~70 tokens;
# a local model that loops must fail in seconds, not at the provider's 4,096-token cap.
RELEVANCE_MAX_TOKENS = 400


class RelevanceResult(BaseModel):
    is_relevant: bool
    confidence: float
    reasoning: str


def build_agent(model: Model) -> Agent[None, RelevanceResult]:
    """Build the stage-1 relevance agent bound to the screening prompt."""
    return Agent(model, result_type=RelevanceResult, system_prompt=PLACEHOLDER_RELEVANCE_PROMPT)


async def run(article: Article, agent: Agent[None, RelevanceResult]) -> RelevanceResult:
    """Screen the removed article for public interest and return the model's judgement."""
    user_prompt = (
        f"URL: {article.url}\nTitle: {article.title}\n"
        f"Content:\n{article.content[:MAX_RELEVANCE_CHARS]}"
    )
    result = await agent.run(user_prompt, model_settings={"max_tokens": RELEVANCE_MAX_TOKENS})
    return result.data
