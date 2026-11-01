from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from core.schemas import NormalizedMessage, NormalizedToolCall


class CheckpointIncompatible(ValueError):
    pass


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    messages: list[NormalizedMessage] = Field(max_length=2000)
    pending_calls: list[NormalizedToolCall] = Field(max_length=100)
    iteration: int = Field(ge=0, le=10)
    total_tool_calls: int = Field(ge=0, le=1000)
    executed_tools: list[tuple[str, str]] = Field(max_length=1000)
    interventions: list[str] = Field(default_factory=list, max_length=5)
    budget: dict = Field(default_factory=dict)
