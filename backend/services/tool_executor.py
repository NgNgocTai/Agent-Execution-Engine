import asyncio
import logging
import time

from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

SKILL_TIMEOUT_SECONDS = 30


class ToolExecutor:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    async def execute(self, tool_name: str, arguments: dict) -> SkillResult:
        skill = self.registry.get(tool_name)

        if skill is None:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error=f"Tool '{tool_name}' không tồn tại.",
            )

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                skill.execute(**arguments),
                timeout=SKILL_TIMEOUT_SECONDS,
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            return result.model_copy(update={"execution_time_ms": elapsed_ms})

        except asyncio.TimeoutError:
            logger.warning("Tool '%s' timeout after %ds", tool_name, SKILL_TIMEOUT_SECONDS)
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=f"Tool '{tool_name}' timeout sau {SKILL_TIMEOUT_SECONDS}s.",
                execution_time_ms=SKILL_TIMEOUT_SECONDS * 1000,
            )

        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("Tool '%s' exception: %s", tool_name, e)
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=f"Lỗi không xác định: {e}",
                execution_time_ms=elapsed_ms,
            )
