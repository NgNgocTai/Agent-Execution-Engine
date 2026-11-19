"""
Langfuse tracing for agent runs (Langfuse Python SDK v5, OpenTelemetry based).

One run is one trace: a root "agent-run" observation with a generation per LLM call and a
tool observation per tool call. The v2 ingestion API this module used to call was shut down
on Langfuse Cloud on 2026-11-16 and rejected every event silently, so this is written
against the current SDK.

Two rules of the OpenTelemetry model matter here:
- An observation is exported only when it ends. `TraceContext.close()` therefore ends
  whatever is still open, so runs that fail, are cancelled or wait for approval are traced
  too (the engine calls it from a `finally`).
- Trace-level attributes (session id, trace name, metadata) are propagated to each
  observation when it starts, so every observation is created inside
  `propagate_attributes`.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel

from core.config import settings
from core.redaction import redact
from core.request_context import request_id, run_id, step_identifier

logger = logging.getLogger(__name__)

MAX_ATTRIBUTE_CHARS = 200  # Langfuse drops propagated string values longer than this


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


def _attribute(value: Any) -> str:
    return str(redact(str(value)))[:MAX_ATTRIBUTE_CHARS]


class LangfuseTracer:
    """Thin wrapper around the Langfuse client with the API the AgentEngine uses."""

    def __init__(self, client: Any = None, propagate: Any = None) -> None:
        self._client = client
        self._propagate = propagate
        self._enabled = client is not None
        if client is not None:
            return
        if not (settings.langfuse_public_key and settings.langfuse_secret_key):
            logger.info("Langfuse tracing disabled (no keys configured)")
            return
        try:
            from langfuse import Langfuse, propagate_attributes

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
            self._propagate = propagate_attributes
            self._enabled = True
            logger.info("Langfuse tracing enabled (host=%s)", settings.langfuse_host)
        except Exception as e:
            logger.warning("Langfuse init failed, tracing disabled: %s", e)
            self._client, self._enabled = None, False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def create_trace(self, session_id: str, user_message: str, provider_name: str) -> TraceContext:
        """One trace per run (or per resumed run segment)."""
        if not self._enabled or not self._client:
            return TraceContext(trace=None, enabled=False)
        attributes = {
            "session_id": _attribute(session_id),
            "trace_name": "agent-run",
            "metadata": {
                key: _attribute(value)
                for key, value in {
                    "provider": provider_name,
                    "request_id": request_id.get(),
                    "run_id": run_id.get(),
                    "conversation_id": session_id,
                }.items()
                if value
            },
        }
        context = TraceContext(trace=None, enabled=True, propagate=self._propagate, attributes=attributes)
        root = context.observe(
            self._client.start_observation,
            name="agent-run",
            as_type="agent",
            input=_json_safe({"user_message": user_message}),
            metadata={"provider": provider_name},
        )
        if root is None:
            return TraceContext(trace=None, enabled=False)
        context._trace = root
        return context

    def flush(self) -> None:
        if self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.warning("Langfuse flush error: %s", e)

    def close(self):
        if self._client:
            try:
                self._client.shutdown()
            except Exception as e:
                logger.warning("Langfuse shutdown error: %s", e)


class TraceContext:
    """The observations of one run. Every method is best effort and never raises."""

    def __init__(self, trace: Any, enabled: bool, propagate: Any = None, attributes: dict | None = None) -> None:
        self._trace = trace
        self._enabled = enabled
        self._propagate = propagate
        self._attributes = attributes or {}
        self._open: list[Any] = []
        self._finalized = False

    def observe(self, start, **kwargs):
        """Start an observation with the trace attributes propagated onto it."""
        try:
            if self._propagate is not None:
                with self._propagate(**self._attributes):
                    return start(**kwargs)
            return start(**kwargs)
        except Exception:
            logger.warning("Telemetry enqueue failed")
            return None

    def _child(self, **kwargs):
        if not self._enabled or not self._trace or self._finalized:
            return None
        observation = self.observe(self._trace.start_observation, **kwargs)
        if observation is not None:
            self._open.append(observation)
        return observation

    def _closed(self, observation) -> None:
        if observation in self._open:
            self._open.remove(observation)

    def start_llm_generation(
        self,
        iteration: int,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> GenerationContext:
        generation = self._child(
            name=f"llm-call-iter-{iteration}",
            as_type="generation",
            model=model,
            input=_json_safe(messages),
            metadata={
                "tools_count": len(tools) if tools else 0,
                "tools": _json_safe(tools or []),
                "step_id": step_identifier(run_id.get(), f"llm:{iteration}"),
            },
        )
        return GenerationContext(generation=generation, enabled=generation is not None, on_end=self._closed)

    def start_tool_span(
        self,
        tool_name: str,
        arguments: dict,
        iteration: int,
        call_id: str | None = None,
    ) -> SpanContext:
        span = self._child(
            name=f"tool-{tool_name}",
            as_type="tool",
            input=_json_safe(arguments),
            metadata={
                "iteration": iteration,
                "step_id": step_identifier(run_id.get(), "tool:" + call_id) if call_id else None,
                "execution_id": step_identifier(run_id.get(), "execution:" + call_id) if call_id else None,
            },
        )
        return SpanContext(span=span, enabled=span is not None, on_end=self._closed)

    def finalize(self, total_tool_calls: int, total_duration_ms: float, output: str | None = None) -> None:
        """Record the run summary and end the trace. Later calls are ignored."""
        if not self._enabled or not self._trace or self._finalized:
            return
        self._end_open_children("Ended with the run before it completed")
        try:
            self._trace.update(
                output=_json_safe(output or "completed"),
                metadata={"total_tool_calls": total_tool_calls, "total_duration_ms": round(total_duration_ms, 2)},
            )
        except Exception as e:
            logger.warning("Langfuse trace finalize error: %s", e)
        self._end_root()

    def close(self) -> None:
        """End anything still open: a run that failed, was cancelled or stopped mid-way."""
        if not self._enabled or not self._trace or self._finalized:
            return
        self._end_open_children("Run stopped before this step completed")
        try:
            self._trace.update(
                level="WARNING",
                status_message="Run ended without a final answer (error, cancellation or worker stop)",
            )
        except Exception as e:
            logger.warning("Langfuse trace close error: %s", e)
        self._end_root()

    def _end_open_children(self, message: str) -> None:
        for observation in list(self._open):
            try:
                observation.update(level="WARNING", status_message=message)
                observation.end()
            except Exception as e:
                logger.warning("Langfuse observation close error: %s", e)
            self._closed(observation)

    def _end_root(self) -> None:
        self._finalized = True
        try:
            self._trace.end()
        except Exception as e:
            logger.warning("Langfuse trace end error: %s", e)


class GenerationContext:
    """One LLM call."""

    def __init__(self, generation: Any, enabled: bool, on_end=None) -> None:
        self._generation = generation
        self._enabled = enabled
        self._on_end = on_end
        self._start_time = time.monotonic()

    def end(
        self,
        output: str | None = None,
        tool_calls: list[dict] | None = None,
        usage: dict | None = None,
        thinking_content: str | None = None,
        provider_error: dict | None = None,
    ) -> None:
        if not self._enabled or not self._generation:
            return
        try:
            end_data: dict[str, Any] = {}
            metadata: dict[str, Any] = {
                "thinking_present": bool(thinking_content),
                "thinking_chars": len(thinking_content) if thinking_content else 0,
            }
            if thinking_content and settings.langfuse_trace_thinking_content:
                metadata["thinking_content"] = thinking_content
            if provider_error:
                metadata["provider_error"] = provider_error
                end_data["level"] = "ERROR"
                end_data["status_message"] = provider_error.get("message", "Provider error")
            if output and tool_calls:
                end_data["output"] = {"text": output, "tool_calls": tool_calls}
            elif tool_calls:
                end_data["output"] = {"tool_calls": tool_calls}
            elif output:
                end_data["output"] = output
            if usage:
                end_data["usage_details"] = usage
            end_data["metadata"] = _json_safe(metadata)
            self._generation.update(**_json_safe(end_data))
            self._generation.end()
        except Exception as e:
            logger.warning("Langfuse generation end error: %s", e)
        finally:
            self._enabled = False
            if self._on_end:
                self._on_end(self._generation)


class SpanContext:
    """One tool call."""

    def __init__(self, span: Any, enabled: bool, on_end=None) -> None:
        self._span = span
        self._enabled = enabled
        self._on_end = on_end

    def end(
        self,
        status: str,
        result: Any = None,
        error: str | None = None,
        execution_time_ms: float | None = None,
    ) -> None:
        if not self._enabled or not self._span:
            return
        try:
            output_data = {"status": status, "result": _json_safe(result), "error": error}
            if execution_time_ms is not None:
                output_data["execution_time_ms"] = round(execution_time_ms, 2)
            update = {"output": _json_safe(output_data)}
            if status != "success":
                update["level"] = "WARNING"
            self._span.update(**update)
            self._span.end()
        except Exception as e:
            logger.warning("Langfuse span end error: %s", e)
        finally:
            self._enabled = False
            if self._on_end:
                self._on_end(self._span)
