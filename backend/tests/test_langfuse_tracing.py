import contextlib
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from core.schemas import NormalizedMessage, NormalizedResponse, NormalizedToolCall
from services.agent_service import AgentEngine
from services.skills.base import SkillResult, SkillStatus, ToolSchema
from tracing.langfuse_tracer import GenerationContext, LangfuseTracer, TraceContext


class FakeObservation:
    """Mimics a Langfuse v5 observation: start children, update, end (export happens on end)."""

    def __init__(self, log, kwargs, propagated):
        self.log, self.kwargs, self.propagated = log, kwargs, propagated
        self.children, self.updates, self.ended = [], [], 0

    def start_observation(self, **kwargs):
        child = FakeObservation(self.log, kwargs, dict(self.log.active))
        self.children.append(child)
        return child

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self

    def end(self):
        self.ended += 1
        return self

    def merged(self):
        result = {}
        for update in self.updates:
            result.update(update)
        return result


class FakeClient:
    def __init__(self):
        self.active = {}
        self.roots = []
        self.flushes = 0

    def start_observation(self, **kwargs):
        root = FakeObservation(self, kwargs, dict(self.active))
        self.roots.append(root)
        return root

    def propagate(self, **attributes):
        @contextlib.contextmanager
        def scope():
            previous, self.active = self.active, attributes
            try:
                yield
            finally:
                self.active = previous

        return scope()

    def flush(self):
        self.flushes += 1


def fake_tracer():
    client = FakeClient()
    return LangfuseTracer(client=client, propagate=client.propagate), client


class FakeSkillRegistry:
    def get_tool_schemas(self):
        return [ToolSchema(name="alarm_statistics", description="Alarm statistics",
                           parameters={"type": "object", "properties": {}})]


class FakeToolExecutor:
    async def execute(self, tool_name, arguments):
        return SkillResult(status=SkillStatus.SUCCESS,
                           data={"ok": True, "tool_name": tool_name, "arguments": arguments},
                           execution_time_ms=12.34)


class ToolCallingProvider:
    model_name = "fake-model"

    def __init__(self):
        self.call_count = 0

    async def chat_completion(self, messages, tools, stream=True):
        self.call_count += 1
        if self.call_count == 1:
            yield NormalizedResponse(
                thinking_content="Need alarm stats.",
                tool_calls=[NormalizedToolCall(call_id="call-1", tool_name="alarm_statistics",
                                               arguments={"window_min": 60})],
                token_usage={"input": 10, "output": 5},
                stop_reason="tool_calls",
            )
            return
        yield NormalizedResponse(text_content="Alarm statistics are ready.",
                                 token_usage={"input": 20, "output": 8}, stop_reason="end_turn")


class FailingProvider:
    model_name = "fake-model"

    async def chat_completion(self, messages, tools, stream=True):
        raise RuntimeError("network down")
        yield  # pragma: no cover  (makes this an async generator)


async def run_engine(tracer, provider):
    engine = AgentEngine(tool_executor=FakeToolExecutor(), skill_registry=FakeSkillRegistry(), tracer=tracer)
    events = []
    try:
        async for event in engine.run(session_id="session-1", user_message="thống kê alarm",
                                      provider=provider, history=[]):
            events.append(event)
    except Exception as exc:  # the engine may surface provider failures; the trace must still close
        events.append(exc)
    return events


class LangfusePayloadTest(unittest.TestCase):
    def test_generation_input_is_normalized_and_trace_attributes_are_propagated(self):
        tracer, client = fake_tracer()
        trace_ctx = tracer.create_trace("session-1", "hello", "FakeProvider")
        trace_ctx.start_llm_generation(
            iteration=1, model="fake-model",
            messages=[NormalizedMessage(role="user", content="hello")],
            tools=[ToolSchema(name="alarm_statistics", description="Alarm statistics", parameters={"type": "object"})],
        )
        root = client.roots[0]
        generation = root.children[0]
        self.assertEqual((root.kwargs["name"], root.kwargs["as_type"]), ("agent-run", "agent"))
        self.assertEqual(root.propagated["session_id"], "session-1")
        self.assertEqual(generation.propagated["session_id"], "session-1")
        self.assertEqual(generation.propagated["trace_name"], "agent-run")
        self.assertEqual(generation.kwargs["as_type"], "generation")
        self.assertEqual(generation.kwargs["input"][0]["content"], "hello")
        self.assertEqual(generation.kwargs["metadata"]["tools"][0]["name"], "alarm_statistics")

    def test_generation_end_records_thinking_summary_and_provider_error(self):
        generation = FakeObservation(FakeClient(), {}, {})
        GenerationContext(generation=generation, enabled=True).end(
            output="Lỗi kết nối LLM", thinking_content="Check provider.",
            provider_error={"provider": "OpenAI", "model": "test-model", "message": "rate limited"},
        )
        update = generation.merged()
        self.assertEqual(update["output"], "Lỗi kết nối LLM")
        self.assertTrue(update["metadata"]["thinking_present"])
        self.assertEqual(update["metadata"]["thinking_chars"], len("Check provider."))
        self.assertNotIn("thinking_content", update["metadata"])
        self.assertEqual((update["level"], update["status_message"]), ("ERROR", "rate limited"))
        self.assertEqual(generation.ended, 1)

    def test_disabled_tracer_never_touches_a_client(self):
        ctx = TraceContext(trace=None, enabled=False)
        ctx.start_llm_generation(1, "m", []).end(output="x")
        ctx.start_tool_span("t", {}, 1).end(status="success")
        ctx.finalize(0, 0.0)
        ctx.close()

    def test_failures_inside_the_sdk_never_reach_the_agent(self):
        class Broken(FakeClient):
            def start_observation(self, **kwargs):
                raise OSError("exporter down")

        client = Broken()
        ctx = LangfuseTracer(client=client, propagate=client.propagate).create_trace("s", "m", "p")
        self.assertFalse(ctx._enabled)
        ctx.start_llm_generation(1, "m", []).end(output="x")
        ctx.close()


class LangfuseAgentLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_completed_run_exports_every_observation_once(self):
        tracer, client = fake_tracer()
        events = await run_engine(tracer, ToolCallingProvider())
        self.assertTrue(events)
        root = client.roots[0]
        generations = [c for c in root.children if c.kwargs["as_type"] == "generation"]
        tools = [c for c in root.children if c.kwargs["as_type"] == "tool"]
        self.assertEqual(len(generations), 2)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].kwargs["name"], "tool-alarm_statistics")
        self.assertEqual(tools[0].merged()["output"]["status"], "success")
        self.assertEqual(generations[0].merged()["usage_details"], {"input": 10, "output": 5})
        self.assertTrue(generations[0].merged()["metadata"]["thinking_present"])
        self.assertEqual(root.merged()["metadata"]["total_tool_calls"], 1)
        self.assertNotIn("level", root.merged())
        # Each observation ends exactly once, so each is exported exactly once.
        self.assertEqual([root.ended] + [c.ended for c in root.children], [1, 1, 1, 1])
        self.assertEqual(client.flushes, 0)  # export is in the background, never blocking the run

    async def test_a_failed_run_is_still_exported_and_marked(self):
        tracer, client = fake_tracer()
        await run_engine(tracer, FailingProvider())
        root = client.roots[0]
        self.assertEqual(root.ended, 1)
        self.assertTrue(all(child.ended == 1 for child in root.children))
        merged = root.merged()
        self.assertTrue(merged.get("level") in {"WARNING", "ERROR"} or "output" in merged, merged)

    async def test_a_run_abandoned_mid_stream_still_ends_its_trace(self):
        tracer, client = fake_tracer()
        engine = AgentEngine(tool_executor=FakeToolExecutor(), skill_registry=FakeSkillRegistry(), tracer=tracer)
        stream = engine.run(session_id="s", user_message="m", provider=ToolCallingProvider(), history=[])
        await stream.__anext__()
        await stream.aclose()  # e.g. the worker is cancelled
        root = client.roots[0]
        self.assertEqual(root.ended, 1)
        self.assertEqual(root.merged()["level"], "WARNING")
        self.assertTrue(all(child.ended == 1 for child in root.children))


if __name__ == "__main__":
    unittest.main()
