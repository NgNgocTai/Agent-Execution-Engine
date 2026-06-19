import json
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from core.config import settings
from core.schemas import NormalizedResponse, NormalizedToolCall
from services.providers.base import LLMProvider
from services.skills.base import ToolSchema

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
        self.model_name = settings.openai_model

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

    def build_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        }

    def build_assistant_message_with_tools(
        self,
        tool_calls: list[NormalizedToolCall],
    ) -> dict:
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {
                        "name": tc.tool_name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        }

    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict],
        stream: bool = True,
    ) -> AsyncGenerator[NormalizedResponse, None]:
        try:
            response = await self.client.chat.completions.create(
                model=settings.openai_model,
                messages=messages,
                tools=tools if tools else None,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception as e:
            logger.error("LLM API error: %s", e)
            yield NormalizedResponse(
                text_content=f"Loi ket noi LLM: {e}",
                stop_reason="end_turn",
            )
            return

        tool_call_buffers: dict[int, dict] = {}

        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_buffers:
                        tool_call_buffers[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments_str": "",
                        }
                    
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_call_buffers[idx]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_call_buffers[idx]["arguments_str"] += tc_delta.function.arguments

            if delta.content:
                yield NormalizedResponse(
                    text_content=delta.content,
                    stop_reason="",
                )

            finish_reason = chunk.choices[0].finish_reason if chunk.choices else None
            if finish_reason == "tool_calls":
                yield NormalizedResponse(
                    tool_calls=[
                        NormalizedToolCall(
                            call_id=buf["id"],
                            tool_name=buf["name"].split("<")[0] if buf["name"] else "",
                            arguments=json.loads(buf["arguments_str"]) if buf["arguments_str"] else {},
                        )
                        for buf in tool_call_buffers.values()
                    ],
                    stop_reason="tool_calls",
                )
            elif finish_reason == "stop":
                yield NormalizedResponse(stop_reason="end_turn")
            
            if hasattr(chunk, "usage") and chunk.usage:
                yield NormalizedResponse(
                    token_usage={
                        "input": chunk.usage.prompt_tokens,
                        "output": chunk.usage.completion_tokens,
                    }
                )

