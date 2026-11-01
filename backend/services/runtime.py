import asyncio
from core.config import settings
from services.agent_service import AgentEngine
from services.skills.registry import SkillRegistry
from services.skills.alarm_statistics import AlarmStatisticsSkill
from services.skills.device_inventory_lookup import DeviceInventoryLookupSkill
from services.skills.noc_alarm_enrichment import NocAlarmEnrichmentSkill
from services.skills.node_health_remediate import NodeHealthRemediateSkill
from services.tool_executor import ToolExecutor
from tracing.langfuse_tracer import LangfuseTracer


async def create_runtime():
    if settings.data_mode == "fixture":
        from evals.runner import build_registry
        registry = build_registry()
        return AgentEngine(ToolExecutor(registry), registry), []
    from repositories.clickhouse_repo import AlarmRepository
    from repositories.postgres_repo import DeviceRepository
    alarms = await asyncio.to_thread(AlarmRepository)
    devices = DeviceRepository()
    try:
        await devices.connect()
    except BaseException:
        await asyncio.to_thread(alarms.close)
        raise
    registry = SkillRegistry()
    for skill in (NocAlarmEnrichmentSkill(alarms, devices),
                  AlarmStatisticsSkill(alarms, devices), DeviceInventoryLookupSkill(alarms, devices)):
        registry.register(skill)
    if settings.ssh_user and settings.ssh_allowed_hosts and (settings.ssh_password or settings.ssh_key_path):
        from pathlib import Path
        if settings.ssh_key_path and not Path(settings.ssh_key_path).is_file():
            raise ValueError("Configured SSH key is unavailable")
        if settings.ssh_known_hosts and not Path(settings.ssh_known_hosts).is_file():
            raise ValueError("Configured SSH trust store is unavailable")
        registry.register(NodeHealthRemediateSkill())
    tracer = LangfuseTracer()
    return AgentEngine(ToolExecutor(registry), registry, tracer), [alarms, devices, tracer]


async def close_runtime(resources):
    for resource in resources:
        if asyncio.iscoroutinefunction(resource.close):
            await resource.close()
        else:
            await asyncio.to_thread(resource.close)
