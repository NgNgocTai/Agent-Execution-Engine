import json
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from core.schemas import NormalizedMessage, NormalizedResponse
from services.providers.base import LLMProvider
from services.providers.thinking import (
    format_provider_error,
)
from services.skills.base import ToolSchema

logger = logging.getLogger(__name__)


class DeepSeekProvider(LLMProvider):
    def __init__(self) -> None:
        from services.run_configuration import execution_settings
        self._config = execution_settings().model_copy(deep=True)
        self.client = AsyncOpenAI(
            max_retries=0,
            api_key=self.config.deepseek_api_key,
            base_url=self.config.deepseek_base_url,
        )
        self.model_name = self.config.deepseek_model

    def format_tools(self, tools: list[ToolSchema]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def _convert_to_openai_messages(self, messages: list[NormalizedMessage]) -> list[dict]:
        openai_msgs = []
        for msg in messages:
            if msg.role == "tool":
                openai_msgs.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content or "",
                })
            elif msg.role == "assistant" and msg.tool_calls:
                openai_msgs.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {
                                "name": tc.tool_name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                })
            else:
                openai_msgs.append({
                    "role": msg.role,
                    "content": msg.content,
                })
        return openai_msgs

    async def chat_completion(
        self,
        messages: list[NormalizedMessage],
        tools: list[ToolSchema],
        stream: bool = True,
    ) -> AsyncGenerator[NormalizedResponse, None]:
        openai_messages = self._convert_to_openai_messages(messages)
        openai_tools = self.format_tools(tools) if tools else None

        try:
            response = await self.client.chat.completions.create(
                model=self.model_name,
                messages=openai_messages,
                tools=openai_tools,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception as e:
            logger.error("DeepSeek API error: %s", e)
            error_message = format_provider_error(e, "DeepSeek", self.model_name)
            yield NormalizedResponse(
                text_content=error_message,
                stop_reason="end_turn",
                provider_error={
                    "provider": "DeepSeek",
                    "model": self.model_name,
                    "error_type": type(e).__name__,
                    "message": error_message,
                },
            )
            return

        from services.providers.completion import openai_chunks
        async for item in openai_chunks(response):
            yield item
