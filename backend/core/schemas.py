from enum import Enum
from typing import Any, TypeAlias
from uuid import uuid4

import time
from pydantic import BaseModel, Field


class AgentEventType(str, Enum):
    THINKING = "thinking"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"
    TEXT_DELTA = "text_delta"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"


EventData: TypeAlias = dict[str, Any]


class AgentEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    type: AgentEventType
    data: EventData
    timestamp_ms: float = Field(default_factory=lambda: time.time() * 1000)
    session_id: str | None = None
    iteration: int | None = None

    def to_sse(self) -> str:
        return f"data: {self.model_dump_json()}\n\n"


class NormalizedToolCall(BaseModel):
    call_id: str
    tool_name: str
    arguments: dict


class NormalizedMessage(BaseModel):
    role: str
    content: str | None = None
    thinking_content: str | None = None
    tool_calls: list[NormalizedToolCall] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False


class NormalizedResponse(BaseModel):
    tool_calls: list[NormalizedToolCall] = []
    text_content: str | None = None
    thinking_content: str | None = None
    stop_reason: str = ""
    token_usage: dict | None = None
    provider_error: dict | None = None
