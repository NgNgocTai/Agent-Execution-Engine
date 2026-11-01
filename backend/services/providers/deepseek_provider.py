import json
import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from core.config import settings
from core.schemas import NormalizedMessage, NormalizedResponse, NormalizedToolCall
from services.providers.base import LLMProvider
from services.providers.thinking import (
    ThinkTagParser,
    format_provider_error,
    get_reasoning_delta,
    parse_tool_arguments,
)
from services.skills.base import ToolSchema

logger = logging.getLogger(__name__)


class DeepSeekProvider(LLMProvider):
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            max_retries=0,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
        self.model_name = settings.deepseek_model

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

        tool_call_buffers: dict[int, dict] = {}
        think_parser = ThinkTagParser()

        async for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                yield NormalizedResponse(token_usage={"input": chunk.usage.prompt_tokens, "output": chunk.usage.completion_tokens})
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_buffers:
                        tool_call_buffers[idx] = {"id": tc_delta.id or "", "name": "", "arguments_str": ""}
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_call_buffers[idx]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_call_buffers[idx]["arguments_str"] += tc_delta.function.arguments

            # OpenRouter trả thinking ở field "reasoning" riêng (không phải <think> tags)
            reasoning = get_reasoning_delta(delta)
            if reasoning:
                yield NormalizedResponse(thinking_content=reasoning)
                continue

            if delta.content:
                for kind, content in think_parser.feed(delta.content):
                    if kind == "thinking":
                        yield NormalizedResponse(thinking_content=content)
                    else:
                        yield NormalizedResponse(text_content=content, stop_reason="")

            finish_reason = chunk.choices[0].finish_reason if chunk.choices else None
            if finish_reason == "tool_calls":
                for kind, content in think_parser.flush():
                    if kind == "thinking":
                        yield NormalizedResponse(thinking_content=content)
                    else:
                        yield NormalizedResponse(text_content=content, stop_reason="")
                yield NormalizedResponse(
                    tool_calls=[
                        NormalizedToolCall(
                            call_id=buf["id"],
                            tool_name=buf["name"].split("<")[0] if buf["name"] else "",
                            arguments=parse_tool_arguments(
                                buf["arguments_str"],
                                buf["name"],
                            ),
                        )
                        for buf in tool_call_buffers.values()
                    ],
                    stop_reason="tool_calls",
                )
            elif finish_reason == "stop":
                for kind, content in think_parser.flush():
                    if kind == "thinking":
                        yield NormalizedResponse(thinking_content=content)
                    else:
                        yield NormalizedResponse(text_content=content, stop_reason="")
                yield NormalizedResponse(stop_reason="end_turn")
