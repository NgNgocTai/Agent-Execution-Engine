import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.schemas import AgentEventType, NormalizedResponse, NormalizedToolCall
from services.agent_service import AgentEngine
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor


class ProviderFailureTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, responses, raises=False, delay=0):
        class Provider:
            async def chat_completion(self, messages, tools):
                for response in responses:
                    yield response
                if delay:
                    await asyncio.sleep(delay)
                if raises:
                    raise RuntimeError("secret-provider-body")

        registry = SkillRegistry()
        executor = ToolExecutor(registry)
        with patch.object(executor, "execute", wraps=executor.execute) as execute:
            events = [event async for event in AgentEngine(executor, registry).run(
                "test", "hello", Provider(), [],
            )]
            execute.assert_not_called()
        self.assertFalse(any(e.type == AgentEventType.FINAL_ANSWER for e in events))
        self.assertEqual(events[-1].type, AgentEventType.ERROR)
        self.assertNotIn("secret-provider-body", events[-1].to_sse())
        return events[-1]

    async def test_provider_error_text_is_not_streamed_as_an_answer(self):
        result = await self._run([NormalizedResponse(
            text_content="secret-provider-body", provider_error={"message": "private"},
        )])
        self.assertFalse(result.data["partial"])
        self.assertEqual(result.data["new_messages"], [])

    async def test_partial_stream_exception_never_dispatches_pending_tools(self):
        result = await self._run([
            NormalizedResponse(text_content="partial"),
            NormalizedResponse(tool_calls=[NormalizedToolCall(
                call_id="call", tool_name="write", arguments={},
            )]),
        ], raises=True)
        self.assertTrue(result.data["partial"])

    async def test_total_provider_deadline_is_enforced(self):
        with patch("services.providers.gateway.settings.llm_timeout_seconds", 0.02):
            result = await self._run([], delay=1)
        self.assertEqual(result.data["code"], "PROVIDER_TIMEOUT")

    async def test_private_reasoning_without_an_answer_is_not_success(self):
        result = await self._run([NormalizedResponse(thinking_content="secret-provider-body")])
        self.assertEqual(result.data["code"], "PROVIDER_NO_ANSWER")
