import json
import logging
import time
from typing import AsyncGenerator
from datetime import datetime, timedelta, timezone

from core.event_emitter import EventEmitter
from core.auth import require
from core.schemas import AgentEvent, NormalizedMessage, NormalizedToolCall
from services.history import build_messages
from services.prompts import get_system_prompt
from services.providers.base import LLMProvider
from services.providers.gateway import complete
from services.run_budget import RunBudget, BudgetExceeded
from services.context_manager import ContextBudgetExceeded
from services.skills.registry import SkillRegistry
from services.grounding import check_answer
from core.config import settings
from services.skill_guides import GUIDE_TOOLS, instructions_loaded
from services.tool_executor import ToolExecutor
from services.skills.base import SkillStatus
from services.skills.base import SkillResult
from tracing.langfuse_tracer import LangfuseTracer

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 10


def _run_start(messages, user_message):
    """Index of this run's user message, so earlier runs' loads do not count."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user" and messages[index].content == user_message:
            return index
    return 0


class AgentEngine:
    def __init__(
        self,
        tool_executor: ToolExecutor,
        skill_registry: SkillRegistry,
        tracer: LangfuseTracer | None = None,
        required_skill: str | None = None,
    ) -> None:
        self.tool_executor = tool_executor
        self.skill_registry = skill_registry
        self.tracer = tracer
        # Operator-selected skill: its instructions must be read before any
        # other tool is used in the run.
        self.required_skill = required_skill

    async def run(self, *args, **kwargs) -> AsyncGenerator[AgentEvent, None]:
        """Run the agent; the run's trace is always closed, even on error or cancellation.

        Tracing exports an observation only when it ends, so a run that raises, is
        cancelled or returns early without finalizing would otherwise leave no trace.
        """
        holder: dict = {}
        try:
            async for event in self._run(*args, trace_holder=holder, **kwargs):
                yield event
        finally:
            trace_ctx = holder.get("trace")
            if trace_ctx is not None:
                trace_ctx.close()

    async def _run(
        self,
        session_id: str,
        user_message: str,
        provider: LLMProvider,
        history: list[dict],
        approval_repo=None,
        resume: bool = False,
        checkpoint: dict | None = None,
        save_checkpoint=None,
        trace_holder: dict | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        start_time = time.monotonic()
        total_tool_calls = 0
        iteration = 0

        emitter = EventEmitter(session_id)
        messages = build_messages(
            history,
            user_message,
            get_system_prompt(),
            append_user=not resume,
        )
        # The provider context may contain only a truncated part of DB history.
        # Track the boundary here rather than slicing by the original history size.
        new_messages_start = len(messages)
        if self.required_skill and checkpoint is None:
            messages[0] = messages[0].model_copy(
                update={
                    "content": (messages[0].content or "")
                    + f"\n\nThe operator selected skill '{self.required_skill}'. "
                    f"First call load_skill with skill_name '{self.required_skill}', "
                    "follow its instructions, and use only that skill's tools."
                }
            )
        tools = self.skill_registry.get_tool_schemas()

        global_executed_tools = set()
        budget = RunBudget(checkpoint.get("budget") if checkpoint else None)
        interventions = []
        resumed_calls = None
        if checkpoint is not None:
            if checkpoint.get("version") != 1:
                yield emitter.error("Unsupported checkpoint version", "CHECKPOINT_VERSION")
                return
            messages = [NormalizedMessage.model_validate(message) for message in checkpoint["messages"]]
            new_messages_start = len(messages)
            iteration = checkpoint["iteration"]
            total_tool_calls = checkpoint["total_tool_calls"]
            global_executed_tools = {tuple(item) for item in checkpoint["executed_tools"]}
            resumed_calls = [NormalizedToolCall.model_validate(call) for call in checkpoint["pending_calls"]]
            interventions = checkpoint.get("interventions", [])


        # --- Langfuse: Tạo Trace cho toàn bộ phiên làm việc ---
        provider_name = type(provider).__name__
        trace_ctx = (
            self.tracer.create_trace(session_id, user_message, provider_name)
            if self.tracer
            else None
        )
        if trace_holder is not None:
            trace_holder["trace"] = trace_ctx

        while iteration < MAX_ITERATIONS or resumed_calls:
            require("operator")
            if resumed_calls:
                pending_tool_calls = resumed_calls
                resumed_calls = None
                accumulated_text = ""
                accumulated_thinking = ""
            else:
                iteration += 1
                pending_tool_calls: list[NormalizedToolCall] = []
                accumulated_text = ""
                accumulated_thinking = ""
                provider_error = None

                # --- Langfuse: Bắt đầu ghi nhận LLM Call ---
                model_name = getattr(provider, "model_name", "unknown")
                gen_ctx = (
                    trace_ctx.start_llm_generation(iteration, model_name, messages, tools)
                    if trace_ctx
                    else None
                )

                total_token_usage = None
                try:
                    budget.reserve(provider, messages, tools)
                except (BudgetExceeded, ContextBudgetExceeded) as exc:
                    error = emitter.error("Run budget or context limit reached", getattr(exc, "code", "CONTEXT_BUDGET_EXCEEDED"))
                    error.data["new_messages"] = messages[new_messages_start:]
                    yield error
                    return
                yield emitter.progress("Preparing model call", iteration, step_key=f"llm:{iteration}", status="running", kind="llm")

                async for response in complete(provider, messages, tools):
                    if response.token_usage:
                        total_token_usage = response.token_usage

                    if response.provider_error:
                        provider_error = response.provider_error
                        break

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

                    if response.thinking_content:
                        if not accumulated_thinking:
                            yield emitter.thinking(content="Analyzing the request", iteration=iteration)
                        accumulated_thinking += response.thinking_content

                budget.record(total_token_usage, accumulated_text, accumulated_thinking, pending_tool_calls)
                yield emitter.progress("Model step completed", iteration, step_key=f"llm:{iteration}",
                    status="failed" if provider_error or (not accumulated_text and not pending_tool_calls) else "succeeded", kind="llm", usage=total_token_usage, budget=budget.snapshot())

                # --- Langfuse: Kết thúc LLM Call, ghi output ---
                if gen_ctx:
                    gen_ctx.end(
                        output=accumulated_text or None,
                        tool_calls=[
                            {"name": tc.tool_name, "arguments": tc.arguments}
                            for tc in pending_tool_calls
                        ] if pending_tool_calls else None,
                        usage=total_token_usage,
                        thinking_content=accumulated_thinking or None,
                        provider_error=provider_error,
                    )

                if provider_error:
                    error = emitter.error(
                        message="Provider request failed. Please try again.",
                        code=provider_error["code"],
                    )
                    error.data["partial"] = bool(accumulated_text)
                    # Keep earlier completed tool batches; never save provider error
                    # text or an incomplete assistant/tool batch as a success.
                    error.data["new_messages"] = messages[new_messages_start:]
                    yield error
                    return

                if accumulated_text:
                    # Redact a complete bounded output instead of independent
                    # token chunks, where a credential could span two events.
                    yield emitter.text_delta(accumulated_text)

                # Nếu có tool calls mà LLM lười không giải thích -> Tự động tạo reasoning
                if pending_tool_calls:
                    if not accumulated_text and not accumulated_thinking:
                        tool_names = ", ".join(tc.tool_name for tc in pending_tool_calls)
                        fallback_thinking = f"Tiến hành gọi công cụ: {tool_names}..."
                        accumulated_thinking += fallback_thinking
                        yield emitter.thinking(content=fallback_thinking, iteration=iteration)

                if not pending_tool_calls:
                    if not accumulated_text:
                        error = emitter.error("Provider did not return an answer", "PROVIDER_NO_ANSWER")
                        error.data["new_messages"] = messages[new_messages_start:]
                        yield error
                        return
                    if accumulated_text or accumulated_thinking:
                        messages.append(
                            NormalizedMessage(
                                role="assistant",
                                content=accumulated_text or None,
                                thinking_content=None,
                            )
                        )
                    break

                assistant_msg = NormalizedMessage(
                    role="assistant",
                    content=accumulated_text or None,
                    thinking_content=None,
                    tool_calls=pending_tool_calls
                )
                messages.append(assistant_msg)

            for call_index, tc in enumerate(pending_tool_calls):
                tc_key = (tc.tool_name, json.dumps(tc.arguments, sort_keys=True))

                # --- Langfuse: Bắt đầu ghi nhận Tool Call ---
                tool_span = (
                    trace_ctx.start_tool_span(tc.tool_name, tc.arguments, iteration, call_id=tc.call_id)
                    if trace_ctx
                    else None
                )

                if (
                    self.required_skill
                    and tc.tool_name not in GUIDE_TOOLS
                    and not instructions_loaded(
                        messages[_run_start(messages, user_message) :],
                        self.required_skill,
                    )
                ):
                    result = SkillResult(
                        status=SkillStatus.BUSINESS_ERROR,
                        data=None,
                        error=f"Call load_skill with skill_name '{self.required_skill}' and read its instructions before using this tool.",
                    )
                elif tc_key in global_executed_tools:
                    result = SkillResult(
                        status=SkillStatus.BUSINESS_ERROR,
                        data=None,
                        error="SYSTEM_WARNING: Bạn đã gọi tool này với tham số y hệt ở vòng lặp trước! Vui lòng đọc kết quả ở trên, KHÔNG ĐƯỢC GỌI LẠI lần nữa. Hãy trả lời người dùng ngay."
                    )
                else:
                    global_executed_tools.add(tc_key)
                    if hasattr(self.tool_executor, "execute_call"):
                        result = await self.tool_executor.execute_call(tc)
                    else:
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

                if result.needs_approval:
                    if approval_repo is None:
                        yield emitter.error(
                            message="Approval repository is not configured.",
                            code="APPROVAL_REPOSITORY_MISSING",
                        )
                        return

                    expires_at = (
                        datetime.now(timezone.utc).replace(tzinfo=None)
                        + timedelta(minutes=5)
                    )
                    approval_metadata = dict(result.approval_metadata or {})
                    approval_metadata["expires_at"] = expires_at.isoformat()

                    await approval_repo.create_pending(
                        conversation_id=session_id,
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        arguments=tc.arguments,
                        approval_metadata=approval_metadata,
                        expires_at=expires_at,
                    )

                    if save_checkpoint:
                        await save_checkpoint({
                            "version": 1,
                            "messages": [message.model_dump(mode="json") for message in messages],
                            "pending_calls": [call.model_dump(mode="json") for call in pending_tool_calls[call_index:]],
                            "iteration": iteration,
                            "total_tool_calls": total_tool_calls,
                            "executed_tools": [list(key) for key in global_executed_tools],
                            "interventions": interventions,
                            "budget": budget.snapshot(),
                        })
                    total_duration_ms = (time.monotonic() - start_time) * 1000
                    if trace_ctx:
                        trace_ctx.finalize(
                            total_tool_calls=total_tool_calls,
                            total_duration_ms=total_duration_ms,
                            output="APPROVAL_REQUIRED",
                        )

                    yield emitter.approval_required(
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        arguments=tc.arguments,
                        approval=approval_metadata,
                        updated_messages=messages[1:],
                        iteration=iteration,
                        new_messages=messages[new_messages_start:],
                    )
                    return

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
                    content=json.dumps({"data": result.data, "evidence": result.evidence} if result.evidence else result.data or result.error, ensure_ascii=False),
                    is_error=is_err
                )
                messages.append(tool_msg)
            if interventions:
                for intervention in interventions:
                    messages.append(NormalizedMessage(role="user", content="Operator intervention after the completed tool batch: " + intervention))
                interventions = []
                yield emitter.thinking("Operator intervention applied", iteration)
        else:
            # --- Langfuse: Ghi nhận lỗi vượt giới hạn ---
            if trace_ctx:
                trace_ctx.finalize(total_tool_calls, (time.monotonic() - start_time) * 1000, output="MAX_ITERATIONS_REACHED")

            error = emitter.error(
                message=f"Đã đạt giới hạn {MAX_ITERATIONS} bước xử lý.",
                code="MAX_ITERATIONS",
            )
            error.data["new_messages"] = messages[new_messages_start:]
            yield error
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

        grounding = None
        if settings.evidence_enforcement != "off":
            grounding = check_answer(
                accumulated_text, messages[_run_start(messages, user_message) :], user_message
            )
            if settings.evidence_enforcement == "strict" and grounding["status"] not in {
                "grounded",
                "not_applicable",
            }:
                error = emitter.error(
                    "The answer is not supported by this run's tool evidence",
                    "ANSWER_UNGROUNDED",
                )
                error.data["grounding"] = grounding
                error.data["new_messages"] = messages[new_messages_start:]
                yield error
                return

        final = emitter.final_answer(
            total_tool_calls=total_tool_calls,
            total_duration_ms=total_duration_ms,
            updated_messages=updated_messages,
            new_messages=messages[new_messages_start:],
        )
        final.data["budget"] = budget.snapshot()
        if grounding is not None:
            final.data["grounding"] = grounding
        yield final
