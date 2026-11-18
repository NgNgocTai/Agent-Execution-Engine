import asyncio
import logging
import time
import json
from pydantic import ValidationError
from core.policy import Decision, plan
from core.redaction import redact
from services.connectors import ConnectorUnavailable
from services.site_capabilities import SiteError
from services.evidence import evidence
from core.config import settings

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
        if skill.input_model:
            try:
                arguments = skill.input_model.model_validate(arguments).model_dump()
            except ValidationError as exc:
                fields = [".".join(str(part) for part in error["loc"]) or "input" for error in exc.errors()]
                return SkillResult(status=SkillStatus.BUSINESS_ERROR, data={"error_code": "INVALID_TOOL_INPUT", "fields": fields}, error="Invalid tool input: " + ", ".join(fields))
        if plan(tool_name, arguments, brokered=bool(getattr(skill, "capability", None))) == Decision.DENY:
            return SkillResult(status=SkillStatus.BUSINESS_ERROR, data={"error_code": "POLICY_DENIED"}, error="Target is outside the allowed action scope")
        try:
            result = await asyncio.wait_for(
                skill.execute(**arguments),
                timeout=SKILL_TIMEOUT_SECONDS,
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            safe_data = redact(result.data)
            if len(json.dumps(safe_data, ensure_ascii=False, default=str).encode()) > settings.tool_output_max_bytes:
                totals = {key: safe_data[key] for key in ("total_alarms", "total_alarms_in_db", "displayed_alarms", "active_alarm_count") if isinstance(safe_data, dict) and key in safe_data}
                safe_data = {**totals, "truncated": True, "warnings": ["Result exceeded the output budget; narrow the query. Detailed records were withheld."]}
            return result.model_copy(update={"execution_time_ms": elapsed_ms, "data": safe_data, "error": redact(result.error),
                "evidence": evidence(skill, arguments, safe_data) if result.success else None})

        except asyncio.TimeoutError:
            logger.warning("Tool '%s' timeout after %ds", tool_name, SKILL_TIMEOUT_SECONDS)
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=f"Tool '{tool_name}' timeout sau {SKILL_TIMEOUT_SECONDS}s.",
                execution_time_ms=SKILL_TIMEOUT_SECONDS * 1000,
            )

        except SiteError as e:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data={"error_code": e.code, **e.detail},
                error=str(e),
                execution_time_ms=(time.monotonic() - start) * 1000,
            )

        except ConnectorUnavailable as e:
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data={"error_code": "CONNECTOR_UNAVAILABLE", "connector": e.connector},
                error=str(e),
                execution_time_ms=(time.monotonic() - start) * 1000,
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
