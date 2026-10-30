from __future__ import annotations

from typing import TYPE_CHECKING

from core.schemas import AgentEvent, AgentEventType

if TYPE_CHECKING:
    from core.schemas import NormalizedMessage
    from services.skills.base import SkillResult


class EventEmitter:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def thinking(self, content: str, iteration: int) -> AgentEvent:
        return AgentEvent(
            type=AgentEventType.THINKING,
            data={"content": content},
            session_id=self.session_id,
            iteration=iteration,
        )

    def tool_call_start(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict,
        iteration: int,
    ) -> AgentEvent:
        return AgentEvent(
            type=AgentEventType.TOOL_CALL_START,
            data={
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments": arguments,
            },
            session_id=self.session_id,
            iteration=iteration,
        )

    def tool_call_result(
        self,
        call_id: str,
        tool_name: str,
        result: SkillResult,
        iteration: int,
    ) -> AgentEvent:
        return AgentEvent(
            type=AgentEventType.TOOL_CALL_RESULT,
            data={
                "call_id": call_id,
                "tool_name": tool_name,
                "status": result.status,
                "result": result.data,
                "error": result.error,
                "execution_time_ms": result.execution_time_ms,
            },
            session_id=self.session_id,
            iteration=iteration,
        )

    def approval_required(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict,
        approval: dict,
        updated_messages: list[NormalizedMessage | dict],
        iteration: int,
        new_messages: list[NormalizedMessage] | None = None,
    ) -> AgentEvent:
        return AgentEvent(
            type=AgentEventType.APPROVAL_REQUIRED,
            data={
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "approval": approval,
                "updated_messages": updated_messages,
                "new_messages": new_messages or [],
            },
            session_id=self.session_id,
            iteration=iteration,
        )

    def text_delta(self, content: str) -> AgentEvent:
        return AgentEvent(
            type=AgentEventType.TEXT_DELTA,
            data={"content": content},
            session_id=self.session_id,
        )

    def final_answer(
        self,
        total_tool_calls: int,
        total_duration_ms: float,
        updated_messages: list[NormalizedMessage | dict],
        new_messages: list[NormalizedMessage] | None = None,
    ) -> AgentEvent:
        return AgentEvent(
            type=AgentEventType.FINAL_ANSWER,
            data={
                "total_tool_calls": total_tool_calls,
                "total_duration_ms": total_duration_ms,
                "updated_messages": updated_messages,
                "new_messages": new_messages or [],
            },
            session_id=self.session_id,
        )

    def error(self, message: str, code: str | None = None) -> AgentEvent:
        return AgentEvent(
            type=AgentEventType.ERROR,
            data={"message": message, "code": code},
            session_id=self.session_id,
        )
