from abc import ABC, abstractmethod
from pydantic_ai.models import Model


class BaseProvider(ABC):
    @abstractmethod
    def get_model(self) -> Model: ...

    @abstractmethod
    async def check_health(self) -> bool: ...
