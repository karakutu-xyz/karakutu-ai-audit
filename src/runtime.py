"""One composition root for the API, the batch CLIs and the job worker.

`build_runtime(settings)` is the wiring `app.lifespan` used to inline and `run_open_cases.py`
duplicated: provider → model → search provider → stage agents → ONE archive client → IPTC concept
catalog. Same calls, same order, same arguments as before — nothing here shapes a result.

`catalog_loader` / `client_factory` exist so a caller can resolve them from its own module at call
time (`app.lifespan` passes its module-level names, which is what `tests/conftest.py` patches).
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from src.clients import get_client
from src.clients.concepts import ConceptCatalog, load_catalog
from src.config.settings import get_settings
from src.providers import get_provider
from src.search import get_search_provider
from src.workflow.agents import StageAgents, build_stage_agents


@dataclass
class Runtime:
    provider: Any
    model: Any
    agents: StageAgents
    catalog: Optional[ConceptCatalog]
    search_provider: Any
    karakutu_client: Any


async def build_runtime(settings=None, *, catalog_loader: Optional[Callable] = None,
                        client_factory: Optional[Callable] = None) -> Runtime:
    settings = settings if settings is not None else get_settings()
    provider = get_provider(settings.PROVIDER_TYPE)
    model = provider.get_model()
    search_provider = get_search_provider(settings.WEB_SEARCH_PROVIDER)
    agents = build_stage_agents(model)
    # ONE archive client for the whole process (it owns the cached 15-minute JWT; see app.py).
    karakutu_client = (client_factory or get_client)()
    catalog = await (catalog_loader or load_catalog)(karakutu_client, Path(settings.KARAKUTU_CONCEPTS_CACHE))
    return Runtime(provider=provider, model=model, agents=agents, catalog=catalog,
                   search_provider=search_provider, karakutu_client=karakutu_client)
