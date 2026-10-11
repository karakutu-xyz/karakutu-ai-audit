import httpx
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel  # safe at import time: pulls in no `openai`

from src.config.settings import get_settings
from src.providers.base import BaseProvider


class LocalProvider(BaseProvider):
    """Local provider.

    Toggle-gated: with MODEL_ENABLED off (the default) this stays a network-free
    TestModel stub. With it on, it builds an LMStudioModel (a bounded OpenAIModel) pointed
    at an OpenAI-compatible server (MODEL_BASE_URL): LM Studio, or a hosted API.
    """

    def get_model(self) -> Model:
        settings = get_settings()
        if not settings.MODEL_ENABLED:
            return TestModel()

        # Lazy import: src.providers.lmstudio imports `openai` at module load, so keeping it
        # in the enabled branch means `import src.providers.local` works even when `openai`
        # is not installed. The model it builds bounds every call (output cap, per-call
        # timeout, no SDK retries, thinking off) — see that module's docstring for the
        # measured runaway that made this necessary.
        from src.providers.lmstudio import build_lmstudio_model

        return build_lmstudio_model(settings)

    async def check_health(self) -> bool:
        settings = get_settings()
        if not settings.MODEL_ENABLED:
            return True

        try:
            # Hosted APIs (Scaleway) answer /models with 401 without the key; LM Studio ignores it.
            headers = {"Authorization": f"Bearer {settings.MODEL_API_KEY}"} if settings.MODEL_API_KEY else {}
            async with httpx.AsyncClient(timeout=settings.MODEL_HEALTH_TIMEOUT) as client:
                resp = await client.get(f"{settings.MODEL_BASE_URL}/models", headers=headers)
                return resp.is_success  # True iff 2xx
        except Exception:
            # Never raise out of a health check; an unreachable model server is just "unhealthy".
            return False
