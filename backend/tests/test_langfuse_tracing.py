import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from core.schemas import NormalizedMessage, NormalizedResponse, NormalizedToolCall
from services.agent_service import AgentEngine
from services.skills.base import SkillResult, SkillStatus, ToolSchema
from tracing.langfuse_tracer import GenerationContext, TraceContext


class FakeGeneration:
    def __init__(self):
        self.end_calls = []

    def end(self, **kwargs):
        self.end_calls.append(kwargs)


class FakeSpan:
    def __init__(self):
        self.end_calls = []

    def end(self, **kwargs):
        self.end_calls.append(kwargs)


class FakeTrace:
    def __init__(self):
        self.generation_calls = []
        self.span_calls = []
        self.update_calls = []
        self.generations = []
        self.spans = []

    def generation(self, **kwargs):
        generation = FakeGeneration()
        self.generation_calls.append(kwargs)
        self.generations.append(generation)
        return generation

    def span(self, **kwargs):
        span = FakeSpan()
        self.span_calls.append(kwargs)
        self.spans.append(span)
        return span

    def update(self, **kwargs):
        self.update_calls.append(kwargs)


class FakeTracer:
    def __init__(self):
        self.trace = FakeTrace()
        self.create_trace_calls = []
        self.flush_calls = 0

    def create_trace(self, session_id, user_message, provider_name):
        self.create_trace_calls.append(
            {
                "session_id": session_id,
                "user_message": user_message,
                "provider_name": provider_name,
            }
        )
        return TraceContext(trace=self.trace, enabled=True)

    def flush(self):
        self.flush_calls += 1


class FakeSkillRegistry:
    def get_tool_schemas(self):
        return [
            ToolSchema(
                name="alarm_statistics",
                description="Alarm statistics",
                parameters={"type": "object", "properties": {}},
            )
        ]


class FakeToolExecutor:
    async def execute(self, tool_name, arguments):
        return SkillResult(
            status=SkillStatus.SUCCESS,
            data={"ok": True, "tool_name": tool_name, "arguments": arguments},
            execution_time_ms=12.34,
        )


class ToolCallingProvider:
    model_name = "fake-model"

    def __init__(self):
        self.call_count = 0

    async def chat_completion(self, messages, tools, stream=True):
        self.call_count += 1
        if self.call_count == 1:
            yield NormalizedResponse(
                thinking_content="Need alarm stats.",
                tool_calls=[
                    NormalizedToolCall(
                        call_id="call-1",
                        tool_name="alarm_statistics",
                        arguments={"window_min": 60},
                    )
                ],
                token_usage={"input": 10, "output": 5},
                stop_reason="tool_calls",
            )
            return

        yield NormalizedResponse(
            text_content="Alarm statistics are ready.",
            token_usage={"input": 20, "output": 8},
            stop_reason="end_turn",
        )


class LangfusePayloadTest(unittest.TestCase):
    def test_trace_context_normalizes_pydantic_payloads(self):
        fake_trace = FakeTrace()
        trace_ctx = TraceContext(trace=fake_trace, enabled=True)

        trace_ctx.start_llm_generation(
            iteration=1,
            model="fake-model",
            messages=[NormalizedMessage(role="user", content="hello")],
            tools=[
                ToolSchema(
                    name="alarm_statistics",
                    description="Alarm statistics",
                    parameters={"type": "object"},
                )
            ],
        )

        call = fake_trace.generation_calls[0]
        self.assertEqual(call["input"][0]["role"], "user")
        self.assertEqual(call["input"][0]["content"], "hello")
        self.assertEqual(call["metadata"]["tools"][0]["name"], "alarm_statistics")

    def test_generation_end_records_thinking_summary_and_provider_error(self):
        fake_generation = FakeGeneration()
        gen_ctx = GenerationContext(generation=fake_generation, enabled=True)

        gen_ctx.end(
            output="Lỗi kết nối LLM",
            thinking_content="Check provider.",
            provider_error={
                "provider": "OpenAI",
                "model": "test-model",
                "message": "rate limited",
            },
        )

        end_call = fake_generation.end_calls[0]
        metadata = end_call["metadata"]
        self.assertEqual(end_call["output"], "Lỗi kết nối LLM")
        self.assertTrue(metadata["thinking_present"])
        self.assertEqual(metadata["thinking_chars"], len("Check provider."))
        self.assertNotIn("thinking_content", metadata)
        self.assertEqual(metadata["provider_error"]["provider"], "OpenAI")
        self.assertEqual(end_call["level"], "ERROR")
        self.assertEqual(end_call["status_message"], "rate limited")


class LangfuseAgentLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_agent_records_llm_generations_tool_span_finalize_and_flush(self):
        tracer = FakeTracer()
        engine = AgentEngine(
            tool_executor=FakeToolExecutor(),
            skill_registry=FakeSkillRegistry(),
            tracer=tracer,
        )

        events = [
            event
            async for event in engine.run(
                session_id="session-1",
                user_message="thống kê alarm",
                provider=ToolCallingProvider(),
                history=[],
            )
        ]

        self.assertTrue(events)
        self.assertEqual(tracer.create_trace_calls[0]["session_id"], "session-1")
        self.assertEqual(tracer.create_trace_calls[0]["provider_name"], "ToolCallingProvider")
        self.assertEqual(len(tracer.trace.generation_calls), 2)
        self.assertEqual(len(tracer.trace.span_calls), 1)
        self.assertEqual(tracer.trace.span_calls[0]["name"], "tool-alarm_statistics")
        self.assertEqual(tracer.trace.spans[0].end_calls[0]["output"]["status"], "success")
        self.assertTrue(
            tracer.trace.generations[0].end_calls[0]["metadata"]["thinking_present"]
        )
        self.assertEqual(
            tracer.trace.generations[0].end_calls[0]["usage_details"],
            {"input": 10, "output": 5},
        )
        self.assertEqual(
            tracer.trace.generations[0].end_calls[0]["metadata"]["thinking_chars"],
            len("Need alarm stats."),
        )
        self.assertEqual(tracer.trace.update_calls[0]["metadata"]["total_tool_calls"], 1)
        self.assertEqual(tracer.flush_calls, 1)


if __name__ == "__main__":
    unittest.main()
