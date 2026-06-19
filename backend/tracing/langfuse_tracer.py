"""
Langfuse Tracer - Camera giám sát bên trong não Agent.

Ghi lại toàn bộ quá trình Agent hoạt động:
- LLM Calls: model, tokens, thời gian
- Tool Calls: tên tool, tham số, kết quả, thời gian
- Tổng kết: tổng thời gian, tổng tool calls
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from langfuse import Langfuse

from core.config import settings

logger = logging.getLogger(__name__)


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

        trace = self._client.trace(
            name="agent-run",
            session_id=session_id,
            input={"user_message": user_message},
            metadata={"provider": provider_name},
        )
        return TraceContext(trace=trace, enabled=True)

    def flush(self) -> None:
        """Đẩy toàn bộ buffer lên Langfuse cloud."""
        if self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.warning("Langfuse flush error: %s", e)


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

        generation = self._trace.generation(
            name=f"llm-call-iter-{iteration}",
            model=model,
            input=messages,
            metadata={"tools_count": len(tools) if tools else 0},
        )
        return GenerationContext(generation=generation, enabled=True)

    def start_tool_span(
        self,
        tool_name: str,
        arguments: dict,
        iteration: int,
    ) -> SpanContext:
        """Ghi nhận bắt đầu một lần gọi Tool."""
        if not self._enabled or not self._trace:
            return SpanContext(span=None, enabled=False)

        span = self._trace.span(
            name=f"tool-{tool_name}",
            input=arguments,
            metadata={"iteration": iteration},
        )
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
                output=output or "completed",
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
    ) -> None:
        """Kết thúc Generation, ghi output và usage."""
        if not self._enabled or not self._generation:
            return

        try:
            end_data: dict[str, Any] = {}

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
                end_data["usage"] = usage

            self._generation.end(**end_data)
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
                "result": result,
                "error": error,
            }
            if execution_time_ms is not None:
                output_data["execution_time_ms"] = round(execution_time_ms, 2)

            self._span.end(output=output_data)
        except Exception as e:
            logger.warning("Langfuse span end error: %s", e)
