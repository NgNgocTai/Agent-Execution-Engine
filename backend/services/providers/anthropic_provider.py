import json
import logging
from typing import AsyncGenerator

from core.config import settings
from core.schemas import NormalizedMessage, NormalizedResponse, NormalizedToolCall
from services.providers.base import LLMProvider
from services.skills.base import ToolSchema

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    def __init__(self) -> None:
        self.model_name = settings.anthropic_model
        self._use_openai_format = bool(settings.anthropic_base_url)

        if self._use_openai_format:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(
                api_key=settings.anthropic_api_key,
                base_url=settings.anthropic_base_url,
            )
        else:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    def format_tools(self, tools: list[ToolSchema]) -> list[dict]:
        if self._use_openai_format:
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
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
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

    def _convert_to_anthropic_messages(self, messages: list[NormalizedMessage]) -> list[dict]:
        anthropic_msgs = []
        for msg in messages:
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
            if anthropic_msgs and anthropic_msgs[-1]["role"] == role:
                if isinstance(anthropic_msgs[-1]["content"], list):
                    anthropic_msgs[-1]["content"].extend(blocks)
                else:
                    old_text = anthropic_msgs[-1]["content"]
                    anthropic_msgs[-1]["content"] = [{"type": "text", "text": old_text}] + blocks
            else:
                anthropic_msgs.append({"role": role, "content": blocks})
        return anthropic_msgs

    async def chat_completion(
        self,
        messages: list[NormalizedMessage],
        tools: list[ToolSchema],
        stream: bool = True,
    ) -> AsyncGenerator[NormalizedResponse, None]:
        if self._use_openai_format:
            async for resp in self._chat_openai_format(messages, tools):
                yield resp
        else:
            async for resp in self._chat_anthropic_format(messages, tools):
                yield resp

    async def _chat_openai_format(
        self, messages: list[NormalizedMessage], tools: list[ToolSchema]
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
            logger.error("LLM API error: %s", e)
            yield NormalizedResponse(text_content=f"Loi ket noi LLM: {e}", stop_reason="end_turn")
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
                        tool_call_buffers[idx] = {"id": tc_delta.id or "", "name": "", "arguments_str": ""}
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_call_buffers[idx]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_call_buffers[idx]["arguments_str"] += tc_delta.function.arguments
            if delta.content:
                yield NormalizedResponse(text_content=delta.content, stop_reason="")
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
                yield NormalizedResponse(token_usage={"input": chunk.usage.prompt_tokens, "output": chunk.usage.completion_tokens})

    async def _chat_anthropic_format(
        self, messages: list[NormalizedMessage], tools: list[ToolSchema]
    ) -> AsyncGenerator[NormalizedResponse, None]:
        anthropic_messages = self._convert_to_anthropic_messages(messages)
        anthropic_tools = self.format_tools(tools) if tools else []

        try:
            async with self.client.messages.stream(
                model=self.model_name,
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
                            current_tool = {"id": event.content_block.id, "name": event.content_block.name}
                            json_buffer = ""
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield NormalizedResponse(text_content=event.delta.text, stop_reason="")
                        elif event.delta.type == "input_json_delta":
                            json_buffer += event.delta.partial_json
                    elif event.type == "content_block_stop":
                        if current_tool:
                            args = json.loads(json_buffer) if json_buffer else {}
                            yield NormalizedResponse(
                                tool_calls=[NormalizedToolCall(call_id=current_tool["id"], tool_name=current_tool["name"], arguments=args)],
                                stop_reason="tool_calls",
                            )
                            current_tool = None
                    elif event.type == "message_stop":
                        if input_tokens > 0 or output_tokens > 0:
                            yield NormalizedResponse(token_usage={"input": input_tokens, "output": output_tokens})
                        yield NormalizedResponse(stop_reason="end_turn")
        except Exception as e:
            logger.error("Anthropic API error: %s", e)
            yield NormalizedResponse(text_content=f"Lỗi kết nối Anthropic: {e}", stop_reason="end_turn")
            return
