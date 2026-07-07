from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.schemas import AgentEvent, AgentEventType


@dataclass
class EvalCollector:
    """Collect normalized AgentEvents without depending on AgentEngine internals."""

    final_answer: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    approval_events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    stream_events: list[dict[str, Any]] = field(default_factory=list)
    text_chunks: list[str] = field(default_factory=list)
    thinking_chunks: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    token_usage: dict[str, Any] | None = None
    provider_error: dict[str, Any] | None = None

    def record(self, event: AgentEvent) -> None:
        event_payload = event.model_dump(mode="json")
        self.stream_events.append(event_payload)

        if event.type == AgentEventType.TEXT_DELTA:
            content = event.data.get("content") or ""
            self.text_chunks.append(content)
            return

        if event.type == AgentEventType.THINKING:
            content = event.data.get("content") or ""
            self.thinking_chunks.append(content)
            return

        if event.type == AgentEventType.TOOL_CALL_START:
            self.tool_calls.append(
                {
                    "call_id": event.data.get("call_id"),
                    "tool_name": event.data.get("tool_name"),
                    "arguments": event.data.get("arguments") or {},
                    "iteration": event.iteration,
                }
            )
            return

        if event.type == AgentEventType.TOOL_CALL_RESULT:
            self.tool_results.append(
                {
                    "call_id": event.data.get("call_id"),
                    "tool_name": event.data.get("tool_name"),
                    "status": event.data.get("status"),
                    "result": event.data.get("result"),
                    "error": event.data.get("error"),
                    "execution_time_ms": event.data.get("execution_time_ms"),
                    "iteration": event.iteration,
                }
            )
            return

        if event.type == AgentEventType.APPROVAL_REQUIRED:
            self.approval_events.append(
                {
                    "call_id": event.data.get("call_id"),
                    "tool_name": event.data.get("tool_name"),
                    "arguments": event.data.get("arguments") or {},
                    "approval": event.data.get("approval") or {},
                    "iteration": event.iteration,
                }
            )
            return

        if event.type == AgentEventType.FINAL_ANSWER:
            self.latency_ms = event.data.get("total_duration_ms")
            updated_messages = event.data.get("updated_messages") or []
            if updated_messages:
                last_message = updated_messages[-1]
                if isinstance(last_message, dict):
                    self.final_answer = last_message.get("content")
                else:
                    self.final_answer = getattr(last_message, "content", None)
            if not self.final_answer:
                self.final_answer = "".join(self.text_chunks) or None
            return

        if event.type == AgentEventType.ERROR:
            error = {
                "message": event.data.get("message"),
                "code": event.data.get("code"),
            }
            self.errors.append(error)
            self.provider_error = error

    def record_approval_decision(
        self,
        *,
        call_id: str,
        tool_name: str,
        status: str,
        result: dict[str, Any],
        summary: str,
    ) -> None:
        self.tool_results.append(
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "status": status,
                "result": result,
                "error": result.get("stderr") if status != "success" else None,
                "execution_time_ms": 0.0,
                "iteration": 0,
            }
        )
        self.text_chunks.append(summary)
        self.final_answer = summary

    def as_observations(self) -> list[dict[str, Any]]:
        return [
            {
                "tool_name": item["tool_name"],
                "status": item["status"],
                "result": item["result"],
                "error": item["error"],
            }
            for item in self.tool_results
        ]
