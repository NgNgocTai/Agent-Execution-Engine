from abc import ABC, abstractmethod
from typing import AsyncGenerator

from core.schemas import NormalizedResponse, NormalizedToolCall
from services.skills.base import ToolSchema


class LLMProvider(ABC):
    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict],
        stream: bool = True,
    ) -> AsyncGenerator[NormalizedResponse, None]: ...

    @abstractmethod
    def format_tools(self, tools: list[ToolSchema]) -> list[dict]: ...

    @abstractmethod
    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict: ...

    @abstractmethod
    def build_assistant_message_with_tools(
        self,
        tool_calls: list[NormalizedToolCall],
    ) -> dict: ...
