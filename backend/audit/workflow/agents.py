from dataclasses import dataclass
from audit.workflow import classification, relevance, related_removals, source, web_search

@dataclass
class StageAgents:
    classification: object
    relevance: object
    related_removals: object
    source: object
    web_search: object

def build_stage_agents(models, prompts: dict[str, str] | None = None):
    from audit.workflow.prompt_registry import DEFAULTS

    texts = DEFAULTS if prompts is None else prompts
    model = lambda step: models[step] if isinstance(models, dict) else models
    return StageAgents(
        classification.build_agents(model("classification"), texts["classification_coarse"], texts["classification_fine"]),
        relevance.build_agent(model("relevance"), texts["relevance"]),
        related_removals.build_agent(model("related_removals"), texts["related_removals"]),
        source.build_agent(model("source"), texts["source"]),
        web_search.build_agent(model("web_search"), texts["web_search"]),
    )
