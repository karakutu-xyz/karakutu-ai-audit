"""Stage 1 — public-interest screening of the removed article."""

from pydantic import BaseModel
from audit.model import Agent
from audit.model import Model

from audit.domain import Article
from audit.search.fetch import prose_of
from audit.workflow import prompts

# The `PLACEHOLDER_` prefix is a misnomer kept from the earlier auditor; the name is the
# default for `build_agent` below. The default text lives in `prompts.py`; runs use the active
# version stored in the database (see `prompt_registry.py`).
PLACEHOLDER_RELEVANCE_PROMPT = prompts.RELEVANCE_PROMPT

# Cap the article text fed to the screen. `Article.content` is caller-supplied and unbounded;
# a full article made the relevance call take 236s (vs the usual ~20s) and, with arbitrary
# input, is a denial-of-service surface. Stage 1 only judges public-interest SCOPE — the
# title plus a generous lede is plenty; it never needs the whole body. The cap applies to the
# article PROSE: archive articles arrive as raw HTML, and the first 2,000 characters of that can
# be all markup (scripts, menus, subscription boxes) before the first paragraph.
MAX_RELEVANCE_CHARS = 2000
# Output cap per call (merged over the provider default). A screening answer is ~70 tokens;
# a local model that loops must fail in seconds, not at the provider's 4,096-token cap.
RELEVANCE_MAX_TOKENS = 400


class RelevanceResult(BaseModel):
    is_relevant: bool
    confidence: float
    reasoning: str


def build_agent(model: Model, system_prompt: str = PLACEHOLDER_RELEVANCE_PROMPT) -> Agent[None, RelevanceResult]:
    """Build the stage-1 relevance agent bound to the screening prompt."""
    return Agent(model, result_type=RelevanceResult, system_prompt=system_prompt)


async def run(article: Article, agent: Agent[None, RelevanceResult]) -> RelevanceResult:
    """Screen the removed article for public interest and return the model's judgement."""
    content = article.content or ""
    prose = prose_of(content) or content
    user_prompt = (
        f"URL: {article.url}\nTitle: {article.title}\n"
        f"Content:\n{prose[:MAX_RELEVANCE_CHARS]}"
    )
    result = await agent.run(user_prompt, model_settings={"max_tokens": RELEVANCE_MAX_TOKENS})
    return result.data
