"""Editable prompt contract. Source constants remain the byte-exact defaults."""
from dataclasses import dataclass

from audit.workflow import prompts


@dataclass(frozen=True)
class PromptSpec:
    text: str
    step: str
    required_tokens: tuple[str, ...]


REGISTRY = {
    "relevance": PromptSpec(prompts.RELEVANCE_PROMPT, "relevance", ("is_relevant", "confidence", "reasoning", "GUARDRAIL")),
    "classification_coarse": PromptSpec(prompts.CLASSIFICATION_COARSE_PROMPT, "classification", ("top_level_topics", "genre", "geographic_scope", "gist", "GUARDRAIL")),
    "classification_fine": PromptSpec(prompts.CLASSIFICATION_FINE_PROMPT, "classification", ("topics", "confidences", "GUARDRAIL")),
    "source": PromptSpec(prompts.SOURCE_PROMPT, "source", ("source_kind", "source_name", "source_url", "attribution_quote", "GUARDRAIL")),
    "web_search": PromptSpec(prompts.WEB_SEARCH_PROMPT, "web_search", ("matches", "covers_key_facts", "omissions", "dead_same_story", "reasoning", "GUARDRAIL")),
    "related_removals": PromptSpec(prompts.RELATED_REMOVALS_PROMPT, "related_removals", ("NONE",)),
}
DEFAULTS = {name: spec.text for name, spec in REGISTRY.items()}


def validation_codes(name: str, text: str) -> list[str]:
    """Return codes only; never include submitted text in diagnostics."""
    if not isinstance(text, str):
        return ["invalid_string"]
    codes = []
    if not text.strip():
        codes.append("empty_text")
    if len(text) > 20_000:
        codes.append("text_too_long")
    if "\0" in text:
        codes.append("nul_not_allowed")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        codes.append("invalid_utf8")
    codes.extend("missing_token:" + token for token in REGISTRY[name].required_tokens if token not in text)
    return codes
