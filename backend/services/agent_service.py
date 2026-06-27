import json
import logging
import time
from typing import AsyncGenerator

from core.event_emitter import EventEmitter
from core.schemas import AgentEvent, AgentEventType, NormalizedMessage, NormalizedToolCall
from services.history import build_messages
from services.prompts import get_system_prompt
from services.providers.base import LLMProvider
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor
from services.skills.base import SkillStatus
from services.skills.base import SkillResult
from tracing.langfuse_tracer import LangfuseTracer

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 10


class AgentEngine:
    def __init__(
        self,
        tool_executor: ToolExecutor,
        skill_registry: SkillRegistry,
        tracer: LangfuseTracer | None = None,
    ) -> None:
        self.tool_executor = tool_executor
        self.skill_registry = skill_registry
        self.tracer = tracer

    async def run(
        self,
        session_id: str,
        user_message: str,
        provider: LLMProvider,
        history: list[dict],
    ) -> AsyncGenerator[AgentEvent, None]:
        start_time = time.monotonic()
        total_tool_calls = 0
        iteration = 0

        emitter = EventEmitter(session_id)
        messages = build_messages(history, user_message, get_system_prompt())
        tools = self.skill_registry.get_tool_schemas()

        global_executed_tools = set()

        # --- Langfuse: Tạo Trace cho toàn bộ phiên làm việc ---
        provider_name = type(provider).__name__
        trace_ctx = (
            self.tracer.create_trace(session_id, user_message, provider_name)
            if self.tracer
            else None
        )

        while iteration < MAX_ITERATIONS:
            iteration += 1
            pending_tool_calls: list[NormalizedToolCall] = []
            accumulated_text = ""
            accumulated_thinking = ""

            # --- Langfuse: Bắt đầu ghi nhận LLM Call ---
            model_name = getattr(provider, "model_name", "unknown")
            gen_ctx = (
                trace_ctx.start_llm_generation(iteration, model_name, messages, tools)
                if trace_ctx
                else None
            )

            total_token_usage = None

            async for response in provider.chat_completion(messages, tools):
                if response.token_usage:
                    total_token_usage = response.token_usage

                if response.tool_calls:
                    for tc in response.tool_calls:
                        # Deduplicate identical tool calls in the same iteration
                        is_duplicate = any(
                            existing_tc.tool_name == tc.tool_name and existing_tc.arguments == tc.arguments
                            for existing_tc in pending_tool_calls
                        )
                        if not is_duplicate:
                            pending_tool_calls.append(tc)
                            yield emitter.tool_call_start(
                                call_id=tc.call_id,
                                tool_name=tc.tool_name,
                                arguments=tc.arguments,
                                iteration=iteration,
                            )

                if response.text_content:
                    accumulated_text += response.text_content
                    # Streaming ngay lập tức để UI mượt mà, không chờ gom hết
                    yield emitter.text_delta(response.text_content)

                if response.thinking_content:
                    accumulated_thinking += response.thinking_content
                    yield emitter.thinking(content=response.thinking_content, iteration=iteration)

            # --- Langfuse: Kết thúc LLM Call, ghi output ---
            if gen_ctx:
                gen_ctx.end(
                    output=accumulated_text or None,
                    tool_calls=[
                        {"name": tc.tool_name, "arguments": tc.arguments}
                        for tc in pending_tool_calls
                    ] if pending_tool_calls else None,
                    usage=total_token_usage,
                )

            # Nếu có tool calls mà LLM lười không giải thích -> Tự động tạo reasoning
            if pending_tool_calls:
                if not accumulated_text and not accumulated_thinking:
                    tool_names = ", ".join(tc.tool_name for tc in pending_tool_calls)
                    fallback_thinking = f"Tiến hành gọi công cụ: {tool_names}..."
                    accumulated_thinking += fallback_thinking
                    yield emitter.thinking(content=fallback_thinking, iteration=iteration)

            if not pending_tool_calls:
                if accumulated_text or accumulated_thinking:
                    messages.append(
                        NormalizedMessage(
                            role="assistant",
                            content=accumulated_text or None,
                            thinking_content=accumulated_thinking or None,
                        )
                    )
                break

            assistant_msg = NormalizedMessage(
                role="assistant",
                content=accumulated_text or None,
                thinking_content=accumulated_thinking or None,
                tool_calls=pending_tool_calls
            )
            messages.append(assistant_msg)

            for tc in pending_tool_calls:
                tc_key = (tc.tool_name, json.dumps(tc.arguments, sort_keys=True))

                # --- Langfuse: Bắt đầu ghi nhận Tool Call ---
                tool_span = (
                    trace_ctx.start_tool_span(tc.tool_name, tc.arguments, iteration)
                    if trace_ctx
                    else None
                )

                if tc_key in global_executed_tools:
                    result = SkillResult(
                        status=SkillStatus.BUSINESS_ERROR,
                        data=None,
                        error="SYSTEM_WARNING: Bạn đã gọi tool này với tham số y hệt ở vòng lặp trước! Vui lòng đọc kết quả ở trên, KHÔNG ĐƯỢC GỌI LẠI lần nữa. Hãy trả lời người dùng ngay."
                    )
                else:
                    global_executed_tools.add(tc_key)
                    result = await self.tool_executor.execute(tc.tool_name, tc.arguments)
                
                total_tool_calls += 1

                # --- Langfuse: Kết thúc Tool Call, ghi kết quả ---
                if tool_span:
                    tool_span.end(
                        status=result.status,
                        result=result.data,
                        error=result.error,
                        execution_time_ms=result.execution_time_ms,
                    )

                yield emitter.tool_call_result(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    result=result,
                    iteration=iteration,
                )

                is_err = result.status != SkillStatus.SUCCESS
                tool_msg = NormalizedMessage(
                    role="tool",
                    tool_call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    content=json.dumps(result.data or result.error, ensure_ascii=False),
                    is_error=is_err
                )
                messages.append(tool_msg)
        else:
            # --- Langfuse: Ghi nhận lỗi vượt giới hạn ---
            if trace_ctx:
                trace_ctx.finalize(total_tool_calls, (time.monotonic() - start_time) * 1000, output="MAX_ITERATIONS_REACHED")
            if self.tracer:
                self.tracer.flush()

            yield emitter.error(
                message=f"Đã đạt giới hạn {MAX_ITERATIONS} bước xử lý.",
                code="MAX_ITERATIONS",
            )
            return

        total_duration_ms = (time.monotonic() - start_time) * 1000

        updated_messages = messages[1:]

        # --- Langfuse: Tổng kết Trace ---
        if trace_ctx:
            trace_ctx.finalize(
                total_tool_calls=total_tool_calls,
                total_duration_ms=total_duration_ms,
                output=accumulated_text or None,
            )
        if self.tracer:
            self.tracer.flush()

        yield emitter.final_answer(
            total_tool_calls=total_tool_calls,
            total_duration_ms=total_duration_ms,
            updated_messages=updated_messages,
        )
