from abc import ABC, abstractmethod
from typing import AsyncGenerator

from core.schemas import NormalizedMessage, NormalizedResponse, NormalizedToolCall
from services.skills.base import ToolSchema


class LLMProvider(ABC):
    @abstractmethod
    async def chat_completion(
        self,
        messages: list[NormalizedMessage],
        tools: list[ToolSchema],
        stream: bool = True,
    ) -> AsyncGenerator[NormalizedResponse, None]: ...
