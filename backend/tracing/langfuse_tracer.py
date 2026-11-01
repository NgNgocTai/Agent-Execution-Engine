"""
Langfuse Tracer - Camera giám sát bên trong não Agent.

Ghi lại toàn bộ quá trình Agent hoạt động:
- LLM Calls: model, tokens, thời gian
- Tool Calls: tên tool, tham số, kết quả, thời gian
- Tổng kết: tổng thời gian, tổng tool calls
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any

from langfuse import Langfuse
from pydantic import BaseModel

from core.config import settings
from core.redaction import redact
from core.request_context import request_id, run_id, step_identifier

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Convert Pydantic/domain objects to Langfuse-friendly JSON data."""
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(exclude_none=True))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return redact({str(k): _json_safe(v) for k, v in value.items() if k != "thinking_content" or settings.langfuse_trace_thinking_content})
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return redact(value)
    return redact(str(value))


class LangfuseTracer:
    """Wrapper quanh Langfuse SDK, cung cấp API đơn giản cho AgentEngine."""

    def __init__(self) -> None:
        self._enabled = bool(
            settings.langfuse_public_key and settings.langfuse_secret_key
        )
        self._client: Langfuse | None = None

        if self._enabled:
            try:
                self._client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                )
                logger.info("Langfuse tracing enabled (host=%s)", settings.langfuse_host)
            except Exception as e:
                logger.warning("Langfuse init failed, tracing disabled: %s", e)
                self._enabled = False
        else:
            logger.info("Langfuse tracing disabled (no keys configured)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def create_trace(
        self,
        session_id: str,
        user_message: str,
        provider_name: str,
    ) -> TraceContext:
        """Tạo một Trace mới cho mỗi lần user gửi câu hỏi."""
        if not self._enabled or not self._client:
            return TraceContext(trace=None, enabled=False)

        try:
            trace = self._client.trace(
                name="agent-run",
                session_id=session_id,
                input=_json_safe({"user_message": user_message}),
                metadata={"provider": provider_name, "request_id": request_id.get(), "run_id": run_id.get(), "conversation_id": session_id},
            )
        except Exception:
            logger.warning("Telemetry enqueue failed")
            return TraceContext(trace=None, enabled=False)
        return TraceContext(trace=trace, enabled=True)

    def flush(self) -> None:
        """Đẩy toàn bộ buffer lên Langfuse cloud."""
        if self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.warning("Langfuse flush error: %s", e)

    def close(self):
        if self._client:
            self._client.shutdown()


class TraceContext:
    """Context cho một lần chạy Agent, quản lý các Span con."""

    def __init__(self, trace: Any, enabled: bool) -> None:
        self._trace = trace
        self._enabled = enabled

    def start_llm_generation(
        self,
        iteration: int,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> GenerationContext:
        """Ghi nhận bắt đầu một lần gọi LLM."""
        if not self._enabled or not self._trace:
            return GenerationContext(generation=None, enabled=False)

        try:
            generation = self._trace.generation(
                name=f"llm-call-iter-{iteration}",
                model=model,
                input=_json_safe(messages),
                metadata={
                    "tools_count": len(tools) if tools else 0,
                    "tools": _json_safe(tools or []),
                "step_id": step_identifier(run_id.get(), f"llm:{iteration}"),
                },
            )
        except Exception:
            logger.warning("Telemetry enqueue failed")
            return GenerationContext(generation=None, enabled=False)
        return GenerationContext(generation=generation, enabled=True)

    def start_tool_span(
        self,
        tool_name: str,
        arguments: dict,
        iteration: int,
        call_id: str | None = None,
    ) -> SpanContext:
        """Ghi nhận bắt đầu một lần gọi Tool."""
        if not self._enabled or not self._trace:
            return SpanContext(span=None, enabled=False)

        try:
            span = self._trace.span(
                name=f"tool-{tool_name}",
                input=_json_safe(arguments),
                metadata={"iteration": iteration,
                    "step_id": step_identifier(run_id.get(), "tool:" + call_id) if call_id else None,
                    "execution_id": step_identifier(run_id.get(), "execution:" + call_id) if call_id else None},
            )
        except Exception:
            logger.warning("Telemetry enqueue failed")
            return SpanContext(span=None, enabled=False)
        return SpanContext(span=span, enabled=True)

    def finalize(
        self,
        total_tool_calls: int,
        total_duration_ms: float,
        output: str | None = None,
    ) -> None:
        """Cập nhật Trace với thông tin tổng kết."""
        if not self._enabled or not self._trace:
            return

        try:
            self._trace.update(
                output=_json_safe(output or "completed"),
                metadata={
                    "total_tool_calls": total_tool_calls,
                    "total_duration_ms": round(total_duration_ms, 2),
                },
            )
        except Exception as e:
            logger.warning("Langfuse trace finalize error: %s", e)


class GenerationContext:
    """Context cho một lần gọi LLM."""

    def __init__(self, generation: Any, enabled: bool) -> None:
        self._generation = generation
        self._enabled = enabled
        self._start_time = time.monotonic()

    def end(
        self,
        output: str | None = None,
        tool_calls: list[dict] | None = None,
        usage: dict | None = None,
        thinking_content: str | None = None,
        provider_error: dict | None = None,
    ) -> None:
        """Kết thúc Generation, ghi output và usage."""
        if not self._enabled or not self._generation:
            return

        try:
            end_data: dict[str, Any] = {}

            metadata: dict[str, Any] = {}

            if thinking_content:
                metadata["thinking_present"] = True
                metadata["thinking_chars"] = len(thinking_content)
                if settings.langfuse_trace_thinking_content:
                    metadata["thinking_content"] = thinking_content
            else:
                metadata["thinking_present"] = False
                metadata["thinking_chars"] = 0

            if provider_error:
                metadata["provider_error"] = provider_error
                end_data["level"] = "ERROR"
                end_data["status_message"] = provider_error.get("message", "Provider error")

            # Ghi nhận đầy đủ: nếu có cả text VÀ tool_calls thì giữ nguyên cả 2
            if output and tool_calls:
                end_data["output"] = {
                    "text": output,
                    "tool_calls": tool_calls,
                }
            elif tool_calls:
                end_data["output"] = {"tool_calls": tool_calls}
            elif output:
                end_data["output"] = output

            if usage:
                end_data["usage_details"] = usage

            end_data["metadata"] = _json_safe(metadata)

            self._generation.end(**_json_safe(end_data))
        except Exception as e:
            logger.warning("Langfuse generation end error: %s", e)


class SpanContext:
    """Context cho một lần gọi Tool."""

    def __init__(self, span: Any, enabled: bool) -> None:
        self._span = span
        self._enabled = enabled

    def end(
        self,
        status: str,
        result: Any = None,
        error: str | None = None,
        execution_time_ms: float | None = None,
    ) -> None:
        """Kết thúc Span, ghi kết quả Tool."""
        if not self._enabled or not self._span:
            return

        try:
            output_data = {
                "status": status,
                "result": _json_safe(result),
                "error": error,
            }
            if execution_time_ms is not None:
                output_data["execution_time_ms"] = round(execution_time_ms, 2)

            self._span.end(output=_json_safe(output_data))
        except Exception as e:
            logger.warning("Langfuse span end error: %s", e)
