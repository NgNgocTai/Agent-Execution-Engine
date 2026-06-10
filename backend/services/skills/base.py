from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel


class SkillStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_ERROR = "business_error"
    SYSTEM_ERROR = "system_error"


class SkillResult(BaseModel):
    status: SkillStatus
    data: Any
    error: str | None = None
    execution_time_ms: float = 0.0

    @property
    def success(self) -> bool:
        return self.status == SkillStatus.SUCCESS


class ToolSchema(BaseModel):
    name: str
    description: str
    parameters: dict


class BaseSkill(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters_schema(self) -> dict: ...

    def get_tool_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
        )

    @abstractmethod
    async def execute(self, **kwargs: Any) -> SkillResult: ...
