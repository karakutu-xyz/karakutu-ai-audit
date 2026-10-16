"""One place that builds every stage agent, used by app.lifespan AND the batch scripts so the
two can never drift (they did once: the trace harness built its own dict of agents)."""
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.models import Model

from src.workflow import classification, related_removals, relevance, source, web_search
from src.workflow.classification import ClassificationAgents


@dataclass
class StageAgents:
    classification: ClassificationAgents
    relevance: Agent
    related_removals: Agent
    source: Agent
    web_search: Agent


def build_stage_agents(model: Model) -> StageAgents:
    return StageAgents(
        classification=classification.build_agents(model),
        relevance=relevance.build_agent(model),
        related_removals=related_removals.build_agent(model),
        source=source.build_agent(model),
        web_search=web_search.build_agent(model),
    )
