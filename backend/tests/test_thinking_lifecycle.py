import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from core.schemas import AgentEventType, NormalizedResponse
from services.agent_service import AgentEngine
from services.providers.thinking import (
    ThinkTagParser,
    format_provider_error,
    parse_tool_arguments,
)


class FakeProvider:
    async def chat_completion(self, messages, tools, stream=True):
        yield NormalizedResponse(thinking_content="checking ")
        yield NormalizedResponse(text_content="done")
        yield NormalizedResponse(stop_reason="end_turn")


class FakeToolExecutor:
    async def execute(self, tool_name: str, arguments: dict):
        raise AssertionError("No tools should be executed in this smoke test")


class FakeSkillRegistry:
    def get_tool_schemas(self):
        return []


class ThinkingParserTest(unittest.TestCase):
    def test_splits_chunked_think_tags(self):
        parser = ThinkTagParser()

        parts = []
        parts.extend(parser.feed("visible <thi"))
        parts.extend(parser.feed("nk>hidden</thi"))
        parts.extend(parser.feed("nk> answer"))
        parts.extend(parser.flush())

        self.assertEqual(
            parts,
            [
                ("text", "visible "),
                ("thinking", "hidden"),
                ("text", " answer"),
            ],
        )

    def test_malformed_tool_arguments_are_rejected_before_dispatch(self):
        for raw in ("{bad json", "[1, 2]", ""):
            with self.assertRaises(ValueError):
                parse_tool_arguments(raw, "broken_tool")
        self.assertEqual(parse_tool_arguments('{"ok": true}', "good_tool"), {"ok": True})

    def test_provider_rate_limit_error_is_human_readable(self):
        err = Exception(
            "Error code: 429 - {'error': {'message': 'Provider returned error', "
            "'code': 429, 'metadata': {'raw': 'openai/gpt-oss-120b:free is "
            "temporarily rate-limited upstream'}}}"
        )

        message = format_provider_error(err, "OpenAI", "openai/gpt-oss-120b:free")

        self.assertIn("giới hạn tốc độ", message)
        self.assertNotIn("metadata", message)
        self.assertNotIn("{'error'", message)


class ThinkingLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_agent_publishes_progress_without_persisting_private_reasoning(self):
        engine = AgentEngine(
            tool_executor=FakeToolExecutor(),
            skill_registry=FakeSkillRegistry(),
            tracer=None,
        )

        events = [
            event
            async for event in engine.run(
                session_id="test-session",
                user_message="hello",
                provider=FakeProvider(),
                history=[],
            )
        ]

        final_event = next(
            event for event in events if event.type == AgentEventType.FINAL_ANSWER
        )
        assistant_msg = final_event.data["updated_messages"][-1]

        self.assertEqual(assistant_msg.content, "done")
        self.assertIsNone(assistant_msg.thinking_content)
        self.assertNotIn("checking ", "".join(event.to_sse() for event in events))


if __name__ == "__main__":
    unittest.main()
