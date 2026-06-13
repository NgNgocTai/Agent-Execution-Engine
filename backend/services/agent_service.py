import json
import logging
import time
from typing import AsyncGenerator

from core.event_emitter import EventEmitter
from core.schemas import AgentEvent, AgentEventType, NormalizedToolCall
from services.history import build_messages
from services.prompts import get_system_prompt
from services.providers.base import LLMProvider
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor
from services.skills.base import SkillStatus
from services.skills.base import SkillResult
logger = logging.getLogger(__name__)

MAX_ITERATIONS = 10


class AgentEngine:
    def __init__(
        self,
        tool_executor: ToolExecutor,
        skill_registry: SkillRegistry,
        tracer: object | None = None,
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
        tools = provider.format_tools(self.skill_registry.get_tool_schemas())

        global_executed_tools = set()

        while iteration < MAX_ITERATIONS:
            iteration += 1
            pending_tool_calls: list[NormalizedToolCall] = []
            accumulated_text = ""

            async for response in provider.chat_completion(messages, tools):
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

            # Nếu có tool calls mà LLM lười không giải thích -> Tự động tạo reasoning
            if pending_tool_calls:
                if not accumulated_text:
                    tool_names = ", ".join(tc.tool_name for tc in pending_tool_calls)
                    yield emitter.thinking(content=f"Tiến hành gọi công cụ: {tool_names}...", iteration=iteration)

            if not pending_tool_calls:
                if accumulated_text:
                    messages.append({"role": "assistant", "content": accumulated_text})
                break

            assistant_msg = provider.build_assistant_message_with_tools(pending_tool_calls)
            messages.append(assistant_msg)

            for tc in pending_tool_calls:
                tc_key = (tc.tool_name, json.dumps(tc.arguments, sort_keys=True))
                
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

                yield emitter.tool_call_result(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    result=result,
                    iteration=iteration,
                )

                tool_msg = provider.build_tool_result_message(
                    tool_call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    result=json.dumps(result.data or result.error, ensure_ascii=False),
                )
                messages.append(tool_msg)
        else:
            yield emitter.error(
                message=f"Đã đạt giới hạn {MAX_ITERATIONS} bước xử lý.",
                code="MAX_ITERATIONS",
            )
            return

        total_duration_ms = (time.monotonic() - start_time) * 1000

        updated_messages = messages[1:]

        yield emitter.final_answer(
            total_tool_calls=total_tool_calls,
            total_duration_ms=total_duration_ms,
            updated_messages=updated_messages,
        )
