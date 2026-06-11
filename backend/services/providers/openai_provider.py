import json
import logging
from typing import AsyncGenerator

import httpx

from core.config import settings
from core.schemas import NormalizedResponse, NormalizedToolCall
from services.providers.base import LLMProvider
from services.skills.base import ToolSchema

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    def __init__(self) -> None:
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.model = settings.openai_model

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
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                data = response.json()
        except Exception as e:
            logger.error("LLM API error: %s", e)
            yield NormalizedResponse(
                text_content=f"Loi ket noi LLM: {e}",
                stop_reason="end_turn",
            )
            return

        if "error" in data:
            err = data["error"]
            logger.error("LLM API error: %s", err)
            yield NormalizedResponse(
                text_content=f"Loi LLM: {err.get('message', err)}",
                stop_reason="end_turn",
            )
            return

        choice = data["choices"][0]
        message = choice["message"]

        if message.get("tool_calls"):
            yield NormalizedResponse(
                tool_calls=[
                    NormalizedToolCall(
                        call_id=tc["id"],
                        tool_name=tc["function"]["name"],
                        arguments=json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {},
                    )
                    for tc in message["tool_calls"]
                ],
                stop_reason="tool_calls",
            )

        if message.get("content"):
            yield NormalizedResponse(
                text_content=message["content"],
                stop_reason="end_turn",
            )

        if choice.get("finish_reason") == "stop" and not message.get("tool_calls"):
            yield NormalizedResponse(stop_reason="end_turn")
