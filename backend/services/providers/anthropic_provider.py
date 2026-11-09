import json
import logging
from typing import AsyncGenerator

from core.config import settings
from core.schemas import NormalizedMessage, NormalizedResponse
from services.providers.base import LLMProvider
from services.providers.thinking import (
    format_provider_error,
)
from services.skills.base import ToolSchema

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    def __init__(self) -> None:
        from services.run_configuration import execution_settings
        self._config = execution_settings().model_copy(deep=True)
        self.model_name = self.config.anthropic_model
        self._use_openai_format = self.config.resolved_anthropic_protocol == "openai_compatible"

        if self._use_openai_format:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(
                max_retries=0,
                api_key=self.config.anthropic_api_key,
                base_url=self.config.anthropic_base_url,
            )
        else:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=self.config.anthropic_api_key, base_url=self.config.anthropic_base_url or None, max_retries=0)

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
            if msg.role == "system":
                continue
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
        reasoning_max_tokens = max(self.config.anthropic_reasoning_max_tokens, 0)
        max_tokens = max(
            self.config.anthropic_max_tokens,
            reasoning_max_tokens + 1,
        )
        extra_body = (
            {
                "reasoning": {
                    "max_tokens": reasoning_max_tokens,
                    "exclude": False,
                }
            }
            if reasoning_max_tokens > 0
            else None
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model_name,
                messages=openai_messages,
                tools=openai_tools,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=extra_body,
            )
        except Exception as e:
            logger.error("LLM API error: %s", e)
            error_message = format_provider_error(e, "Anthropic", self.model_name)
            yield NormalizedResponse(
                text_content=error_message,
                stop_reason="end_turn",
                provider_error={
                    "provider": "Anthropic",
                    "model": self.model_name,
                    "error_type": type(e).__name__,
                    "message": error_message,
                },
            )
            return

        from services.providers.completion import openai_chunks
        async for item in openai_chunks(response):
            yield item

    async def _chat_anthropic_format(self, messages, tools):
        from services.providers.completion import normalize_stop, completion_error, strict_arguments
        from services.providers.thinking import ThinkTagParser
        from core.schemas import NormalizedToolCall
        parser = ThinkTagParser()
        calls, current_tool, raw, reason = [], None, "", "unknown"
        finished = False
        input_tokens = output_tokens = 0
        try:
            async with self.client.messages.stream(
                model=self.model_name,
                max_tokens=self.config.anthropic_max_tokens,
                system="\n\n".join(message.content or "" for message in messages if message.role == "system"),
                messages=self._convert_to_anthropic_messages(messages),
                tools=self.format_tools(tools) if tools else [],
            ) as stream:
                async for event in stream:
                    if finished:
                        yield NormalizedResponse(provider_error={"code": "PROVIDER_INCOMPLETE"})
                        return
                    if event.type == "message_start":
                        input_tokens = getattr(getattr(event.message, "usage", None), "input_tokens", 0)
                    elif event.type == "message_delta":
                        output_tokens = getattr(getattr(event, "usage", None), "output_tokens", output_tokens)
                        stop = getattr(getattr(event, "delta", None), "stop_reason", None)
                        if stop is not None:
                            reason = normalize_stop(stop)
                    elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                        current_tool = event.content_block
                        raw = ""
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            for kind, text in parser.feed(event.delta.text):
                                yield NormalizedResponse(**{"thinking_content" if kind == "thinking" else "text_content": text})
                        elif event.delta.type == "thinking_delta":
                            yield NormalizedResponse(thinking_content=event.delta.thinking)
                        elif event.delta.type == "input_json_delta":
                            raw += event.delta.partial_json
                            if len(raw.encode()) > self.config.llm_output_max_bytes:
                                yield NormalizedResponse(provider_error={"code": "PROVIDER_OUTPUT_LIMIT"})
                                return
                    elif event.type == "content_block_stop" and current_tool:
                        try:
                            arguments = strict_arguments(raw or json.dumps(getattr(current_tool, "input", {})))
                            if not current_tool.id or not current_tool.name:
                                raise ValueError("Missing tool identity")
                            calls.append(NormalizedToolCall(call_id=current_tool.id, tool_name=current_tool.name, arguments=arguments))
                        except (ValueError, RecursionError):
                            yield NormalizedResponse(provider_error={"code": "PROVIDER_INVALID_TOOL_ARGUMENTS"})
                            return
                        current_tool = None
                        if len(calls) > self.config.llm_tool_batch_limit:
                            yield NormalizedResponse(provider_error={"code": "TOOL_BATCH_LIMIT"})
                            return
                    elif event.type == "message_stop":
                        finished = True
                        for kind, text in parser.flush():
                            yield NormalizedResponse(**{"thinking_content" if kind == "thinking" else "text_content": text})
                        yield NormalizedResponse(token_usage={"input": input_tokens, "output": output_tokens})
                        if reason not in {"end_turn", "tool_calls"} or current_tool or bool(calls) != (reason == "tool_calls"):
                            yield NormalizedResponse(stop_reason=reason, provider_error={"code": completion_error(reason)})
                            return
                        if len({call.call_id for call in calls}) != len(calls):
                            yield NormalizedResponse(provider_error={"code": "PROVIDER_INVALID_TOOL_ARGUMENTS"})
                            return
                        yield NormalizedResponse(tool_calls=calls, stop_reason=reason)
            if not finished:
                yield NormalizedResponse(provider_error={"code": "PROVIDER_INCOMPLETE"})
        except Exception:
            yield NormalizedResponse(provider_error={"code": "PROVIDER_ERROR"})
