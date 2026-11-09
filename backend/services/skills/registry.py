from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.skills.base import BaseSkill, ToolSchema


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill) -> None:
        if skill.name in self._skills:
            raise ValueError("Duplicate tool name in skill catalog")
        self._skills[skill.name] = skill

    def get(self, name: str) -> BaseSkill | None:
        return self._skills.get(name)

    def get_tool_schemas(self) -> list[ToolSchema]:
        return [skill.get_tool_schema() for skill in self._skills.values()]

    def list_names(self) -> list[str]:
        return list(self._skills.keys())
