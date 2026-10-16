import logging
from typing import Dict, List, Type

from src.providers.base import BaseProvider
from src.providers.local import LocalProvider

logger = logging.getLogger(__name__)

PROVIDER_REGISTRY: Dict[str, Type[BaseProvider]] = {
    "local": LocalProvider,
}


def get_provider(name: str) -> BaseProvider:
    key = name.lower().strip()
    if key not in PROVIDER_REGISTRY:
        available = ", ".join(sorted(PROVIDER_REGISTRY.keys()))
        raise ValueError(f"Unknown provider: '{name}'. Available: {available}")
    return PROVIDER_REGISTRY[key]()


def list_available_providers() -> List[str]:
    return sorted(PROVIDER_REGISTRY.keys())


async def check_provider_health(name: str) -> bool:
    try:
        return await get_provider(name).check_health()
    except Exception as e:
        logger.error(f"Health check failed for '{name}': {e}")
        return False


__all__ = [
    "BaseProvider", "LocalProvider",
    "PROVIDER_REGISTRY", "get_provider", "list_available_providers", "check_provider_health",
]
