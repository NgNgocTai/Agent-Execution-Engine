from enum import Enum
from typing import Any, TypeAlias
from uuid import uuid4

import time
import json
from core.redaction import redact
from pydantic import BaseModel, Field


class AgentEventType(str, Enum):
    PROGRESS = "progress"
    THINKING = "thinking"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"
    APPROVAL_REQUIRED = "approval_required"
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
        public = self.model_dump(mode="json")
        return f"data: {json.dumps(redact(public), ensure_ascii=False, separators=(',', ':'))}\n\n"


class NormalizedToolCall(BaseModel):
    call_id: str = Field(min_length=1, max_length=100)
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict


class NormalizedMessage(BaseModel):
    role: str
    content: str | None = None
    thinking_content: str | None = None
    tool_calls: list[NormalizedToolCall] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    source_id: str | None = None


class NormalizedResponse(BaseModel):
    tool_calls: list[NormalizedToolCall] = []
    text_content: str | None = None
    thinking_content: str | None = None
    stop_reason: str = ""
    token_usage: dict | None = None
    provider_error: dict | None = None
