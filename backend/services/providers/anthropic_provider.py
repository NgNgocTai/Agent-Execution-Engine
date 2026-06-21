import json
import logging
from typing import AsyncGenerator

from anthropic import AsyncAnthropic

from core.config import settings
from core.schemas import NormalizedMessage, NormalizedResponse, NormalizedToolCall
from services.providers.base import LLMProvider
from services.skills.base import ToolSchema

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    def __init__(self) -> None:
        self.client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model_name = settings.anthropic_model

    def format_tools(self, tools: list[ToolSchema]) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    def _convert_to_anthropic_messages(self, messages: list[NormalizedMessage]) -> list[dict]:
        anthropic_msgs = []
        for msg in messages:
            # Anthropic chỉ chấp nhận role "user" và "assistant"
            role = "assistant" if msg.role == "assistant" else "user"
            
            blocks = []
            if msg.role == "tool":
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content or "",
                    "is_error": msg.is_error,
                })
            elif msg.role == "assistant" and msg.tool_calls:
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.call_id,
                        "name": tc.tool_name,
                        "input": tc.arguments,
                    })
            else:
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
            
            if not blocks:
                continue
                
            # Tự động gộp các message liên tiếp cùng role để không vi phạm rule của Anthropic
            if anthropic_msgs and anthropic_msgs[-1]["role"] == role:
                if isinstance(anthropic_msgs[-1]["content"], list):
                    anthropic_msgs[-1]["content"].extend(blocks)
                else:
                    old_text = anthropic_msgs[-1]["content"]
                    anthropic_msgs[-1]["content"] = [{"type": "text", "text": old_text}] + blocks
            else:
                anthropic_msgs.append({
                    "role": role,
                    "content": blocks
                })
        return anthropic_msgs

    async def chat_completion(
        self,
        messages: list[NormalizedMessage],
        tools: list[ToolSchema],
        stream: bool = True,
    ) -> AsyncGenerator[NormalizedResponse, None]:
        anthropic_messages = self._convert_to_anthropic_messages(messages)
        anthropic_tools = self.format_tools(tools) if tools else []

        async with self.client.messages.stream(
            model=settings.anthropic_model,
            max_tokens=4096,
            messages=anthropic_messages,
            tools=anthropic_tools,
        ) as stream:
            current_tool: dict | None = None
            json_buffer = ""
            input_tokens = 0
            output_tokens = 0

            async for event in stream:
                if event.type == "message_start":
                    if hasattr(event.message, "usage") and event.message.usage:
                        input_tokens = event.message.usage.input_tokens
                        
                elif event.type == "message_delta":
                    if hasattr(event, "usage") and event.usage:
                        output_tokens = event.usage.output_tokens
                        
                elif event.type == "content_block_start":
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
                    if input_tokens > 0 or output_tokens > 0:
                        yield NormalizedResponse(
                            token_usage={"input": input_tokens, "output": output_tokens}
                        )
                    yield NormalizedResponse(stop_reason="end_turn")
