from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.schemas import (
    NormalizedMessage,
    NormalizedResponse,
    NormalizedToolCall,
    AgentEventType,
)
from services.providers.openai_provider import OpenAIProvider
from services.providers.anthropic_provider import AnthropicProvider
from services.providers.deepseek_provider import DeepSeekProvider
from services.providers.completion import openai_chunks
from services.agent_service import AgentEngine
from services.skills.registry import SkillRegistry


def chunk(reason=None, content=None, arguments=None):
    calls = (
        [NS(index=0, id="call", function=NS(name="write", arguments=arguments))]
        if arguments is not None
        else None
    )
    return NS(
        usage=None,
        choices=[NS(delta=NS(content=content, tool_calls=calls), finish_reason=reason)],
    )


async def items(values):
    for value in values:
        yield value


class CompletionContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_openai_adapters_reject_truncation_filter_eof_and_bad_json(self):
        cases = [
            ([chunk("length", "partial")], "PROVIDER_TRUNCATED"),
            ([chunk("content_filter")], "PROVIDER_FILTERED"),
            ([chunk(content="partial")], "PROVIDER_INCOMPLETE"),
            ([chunk("unexpected", "partial")], "PROVIDER_INCOMPLETE"),
            (
                [chunk("tool_calls", arguments="{bad")],
                "PROVIDER_INVALID_TOOL_ARGUMENTS",
            ),
            ([chunk("tool_calls", arguments="[]")], "PROVIDER_INVALID_TOOL_ARGUMENTS"),
            (
                [chunk("tool_calls", arguments='{"x":1e309}')],
                "PROVIDER_INVALID_TOOL_ARGUMENTS",
            ),
            (
                [chunk("tool_calls", arguments='{"x":NaN}')],
                "PROVIDER_INVALID_TOOL_ARGUMENTS",
            ),
            (
                [chunk("tool_calls", arguments='{"x":1,"x":2}')],
                "PROVIDER_INVALID_TOOL_ARGUMENTS",
            ),
        ]
        for cls in (OpenAIProvider, DeepSeekProvider, AnthropicProvider):
            for chunks, code in cases:
                with self.subTest(adapter=cls.__name__, code=code):
                    adapter = object.__new__(cls)
                    adapter._use_openai_format = True
                    adapter.model_name = "fake"
                    adapter.client = NS(
                        chat=NS(
                            completions=NS(create=AsyncMock(return_value=items(chunks)))
                        )
                    )
                    output = [r async for r in adapter.chat_completion([], [])]
                    self.assertEqual(output[-1].provider_error["code"], code)
                    self.assertFalse(any(r.tool_calls for r in output))

    async def test_valid_final_with_reasoning_and_usage_tail(self):
        final = chunk("stop", "answer")
        final.choices[0].delta.reasoning = "private"
        usage = NS(choices=[], usage=NS(prompt_tokens=2, completion_tokens=3))
        output = [r async for r in openai_chunks(items([final, usage]))]
        self.assertEqual("".join(r.text_content or "" for r in output), "answer")
        self.assertEqual([r.stop_reason for r in output if r.stop_reason], ["end_turn"])
        self.assertEqual(output[-1].token_usage, {"input": 2, "output": 3})

    async def test_engine_does_not_dispatch_partial_batch_or_retry_filtered_completion(
        self,
    ):
        for reason in ("length", "content_filter", "unknown"):

            class Provider:
                calls = 0

                async def chat_completion(self, messages, tools):
                    self.calls += 1
                    yield NormalizedResponse(
                        text_content="partial",
                        tool_calls=[
                            NormalizedToolCall(
                                call_id="call", tool_name="write", arguments={}
                            )
                        ],
                    )
                    yield NormalizedResponse(stop_reason=reason)

            provider = Provider()
            executor = NS(execute_call=AsyncMock())
            events = [
                e
                async for e in AgentEngine(executor, SkillRegistry()).run(
                    "run", "hello", provider, []
                )
            ]
            executor.execute_call.assert_not_awaited()
            self.assertEqual(provider.calls, 1)
            self.assertEqual(events[-1].type, AgentEventType.ERROR)
            self.assertNotIn(AgentEventType.FINAL_ANSWER, [e.type for e in events])
            self.assertEqual(events[-1].data["new_messages"], [])

    async def test_native_waits_for_message_stop_before_exposing_tools(self):
        start = NS(
            type="content_block_start",
            content_block=NS(type="tool_use", id="call", name="write", input={}),
        )
        stop = NS(type="content_block_stop")
        for reason, error in [
            ("max_tokens", "PROVIDER_TRUNCATED"),
            ("refusal", "PROVIDER_FILTERED"),
            ("tool_use", None),
            (None, "PROVIDER_INCOMPLETE"),
        ]:
            events = [
                start,
                stop,
                NS(type="message_delta", delta=NS(stop_reason=reason), usage=None),
                NS(type="message_stop"),
            ]
            context = AsyncMock()
            context.__aenter__.return_value = items(events)
            adapter = object.__new__(AnthropicProvider)
            adapter._use_openai_format = False
            adapter.model_name = "fake"
            adapter.client = NS(messages=NS(stream=lambda **kw: context))
            output = [
                r
                async for r in adapter.chat_completion(
                    [NormalizedMessage(role="system", content="policy")], []
                )
            ]
            if error:
                self.assertEqual(output[-1].provider_error["code"], error)
                self.assertFalse(any(r.tool_calls for r in output))
            else:
                self.assertEqual(output[-1].tool_calls[0].arguments, {})
                self.assertEqual(output[-1].stop_reason, "tool_calls")
