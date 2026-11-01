from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.schemas import NormalizedMessage, NormalizedResponse, NormalizedToolCall
from core.config import settings
from services.context_manager import prepare_context, ContextBudgetExceeded
from services.providers.gateway import complete
import json


class ContextTest(unittest.TestCase):
    def test_compaction_drops_complete_old_turns_instead_of_splitting_tools(self):
        messages = [NormalizedMessage(role="system", content="system"),
            NormalizedMessage(role="user", content="old" * 3000),
            NormalizedMessage(role="assistant", tool_calls=[NormalizedToolCall(call_id="call", tool_name="tool", arguments={})]),
            NormalizedMessage(role="tool", tool_call_id="call", content="result"),
            NormalizedMessage(role="user", content="latest")]
        context = prepare_context(messages, [], 3000, 500, 256)
        self.assertEqual([message.content for message in context], ["system", "latest"])
    def test_oversize_current_turn_fails_before_provider_call(self):
        with self.assertRaises(ContextBudgetExceeded):
            prepare_context([NormalizedMessage(role="user", content="x" * 5000)], [], 2048, 500, 256)
    def test_orphan_historic_call_is_not_sent_to_provider(self):
        messages = [NormalizedMessage(role="user", content="old"),
                    NormalizedMessage(role="assistant", tool_calls=[NormalizedToolCall(call_id="pending", tool_name="write", arguments={})]),
                    NormalizedMessage(role="user", content="new")]
        self.assertEqual([message.content for message in prepare_context(messages, [], 8000, 1000, 256)], ["new"])

    def test_extracts_provenance_and_falls_back_when_summary_fails(self):
        messages = [NormalizedMessage(role="system", content="system"), NormalizedMessage(role="user", content="node-a " + "x" * 10000, source_id="17"), NormalizedMessage(role="user", content="latest", source_id="18")]
        result = prepare_context(messages, [], 5000, 500, 256, summarize=True)
        summary = json.loads(result[1].content)
        self.assertIn("17", summary["source_ids"])
        self.assertIn("node-a", summary["excerpts"][0]["text"])
        with patch("services.context_manager.historical_excerpt", side_effect=RuntimeError("summarizer failed")):
            result = prepare_context(messages, [], 5000, 500, 256, summarize=True)
        self.assertEqual([message.content for message in result], ["system", "latest"])


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_only_before_first_output(self):
        class Good:
            calls = 0
            async def chat_completion(self, messages, tools):
                self.calls += 1
                yield NormalizedResponse(text_content="fallback")
        class Broken:
            def __init__(self, partial): self.partial = partial
            async def chat_completion(self, messages, tools):
                if self.partial: yield NormalizedResponse(text_content="partial")
                yield NormalizedResponse(provider_error={"error_type": "AuthenticationError"})
        for partial in (False, True):
            good, broken = Good(), Broken(partial)
            broken.fallbacks = [good]
            result = [response async for response in complete(broken, [NormalizedMessage(role="user", content="hello")], [])]
            self.assertEqual(good.calls, 0 if partial else 1)
            self.assertEqual([response.text_content for response in result if response.text_content], ["partial"] if partial else ["fallback"])
    async def test_transient_retry_is_bounded_and_auth_is_not_retried(self):
        class Broken:
            calls = 0
            kind = "RateLimitError"
            async def chat_completion(self, messages, tools):
                self.calls += 1
                yield NormalizedResponse(provider_error={"error_type": self.kind})
        for kind, expected in (("RateLimitError", 2), ("AuthenticationError", 1)):
            provider = Broken(); provider.kind = kind
            with patch.object(settings, "llm_max_attempts", 2):
                result = [item async for item in complete(provider, [NormalizedMessage(role="user", content="hello")], [])]
            self.assertEqual(provider.calls, expected)
            self.assertIsNotNone(result[-1].provider_error)
