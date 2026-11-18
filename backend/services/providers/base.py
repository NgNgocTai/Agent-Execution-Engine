from abc import ABC, abstractmethod
from typing import AsyncGenerator

from core.schemas import NormalizedMessage, NormalizedResponse
from services.skills.base import ToolSchema


class LLMProvider(ABC):
    @property
    def config(self):
        from services.run_configuration import execution_settings
        return getattr(self, "_config", None) or execution_settings()

    @abstractmethod
    async def chat_completion(
        self,
        messages: list[NormalizedMessage],
        tools: list[ToolSchema],
        stream: bool = True,
    ) -> AsyncGenerator[NormalizedResponse, None]: ...
