"""Site-scoped built-in skills over the backend-owned capabilities."""

from services.connectors import ConnectorUnavailable  # noqa: F401  (propagates)
from services.site_capabilities import (
    SiteAmbiguous,
    SiteError,
    site_alarms,
    site_inventory,
)
from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.input_models import SiteInput


def site_failure(exc: SiteError):
    """Not found / ambiguous / forbidden are business results, never empty data."""
    data = {"error_code": exc.code}
    if isinstance(exc, SiteAmbiguous):
        data.update(exc.detail)
    return SkillResult(status=SkillStatus.BUSINESS_ERROR, data=data, error=str(exc))


class SiteInventorySkill(BaseSkill):
    input_model = SiteInput

    def __init__(self, hub) -> None:
        self.hub = hub

    @property
    def name(self) -> str:
        return "site_inventory"

    @property
    def description(self) -> str:
        return (
            "List the devices of one site (station id or exact name) from the "
            "inventory. Limited to sites the caller is authorized for."
        )

    @property
    def parameters_schema(self) -> dict:
        return SiteInput.model_json_schema()

    async def execute(self, site: str, limit: int = 50, **kwargs) -> SkillResult:
        try:
            data = await site_inventory(self.hub, site, limit)
        except SiteError as exc:
            return site_failure(exc)
        return SkillResult(status=SkillStatus.SUCCESS, data=data)


class SiteAlarmOverviewSkill(BaseSkill):
    input_model = SiteInput

    def __init__(self, hub) -> None:
        self.hub = hub

    @property
    def name(self) -> str:
        return "site_alarm_overview"

    @property
    def description(self) -> str:
        return (
            "Active alarms for all devices of one site (station id or exact "
            "name). Reports no_devices, no_active_alarms, forbidden and source "
            "outages as different results. Limited to authorized sites."
        )

    @property
    def parameters_schema(self) -> dict:
        return SiteInput.model_json_schema()

    async def execute(self, site: str, limit: int = 50, **kwargs) -> SkillResult:
        try:
            data = await site_alarms(self.hub, site, limit)
        except SiteError as exc:
            return site_failure(exc)
        return SkillResult(status=SkillStatus.SUCCESS, data=data)
