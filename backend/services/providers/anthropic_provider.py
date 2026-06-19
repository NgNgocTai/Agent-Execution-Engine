import json
import logging
from typing import AsyncGenerator

from anthropic import AsyncAnthropic

from core.config import settings
from core.schemas import NormalizedResponse, NormalizedToolCall
from services.providers.base import LLMProvider
from services.skills.base import ToolSchema

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    def __init__(self) -> None:
        self.client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    def format_tools(self, tools: list[ToolSchema]) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
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
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": result,
                }
            ],
        }

    def build_assistant_message_with_tools(
        self,
        tool_calls: list[NormalizedToolCall],
    ) -> dict:
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tc.call_id,
                    "name": tc.tool_name,
                    "input": tc.arguments,
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
        async with self.client.messages.stream(
            model=settings.anthropic_model,
            max_tokens=4096,
            messages=messages,
            tools=tools if tools else [],
        ) as stream:
            current_tool: dict | None = None
            json_buffer = ""

            async for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        current_tool = {
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                        }
                        json_buffer = ""
                        yield NormalizedResponse(
                            tool_calls=[
                                NormalizedToolCall(
                                    call_id=current_tool["id"],
                                    tool_name=current_tool["name"],
                                    arguments={},
                                )
                            ],
                            stop_reason="tool_calls",
                        )

                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield NormalizedResponse(
                            text_content=event.delta.text,
                            stop_reason="",
                        )
                    elif event.delta.type == "input_json_delta":
                        json_buffer += event.delta.partial_json

                elif event.type == "content_block_stop":
                    if current_tool:
                        args = json.loads(json_buffer) if json_buffer else {}
                        yield NormalizedResponse(
                            tool_calls=[
                                NormalizedToolCall(
                                    call_id=current_tool["id"],
                                    tool_name=current_tool["name"],
                                    arguments=args,
                                )
                            ],
                            stop_reason="tool_calls",
                        )
                        current_tool = None

                elif event.type == "message_stop":
                    yield NormalizedResponse(stop_reason="end_turn")
