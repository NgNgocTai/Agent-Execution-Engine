import asyncio
import logging

from services.connectors import ConnectorUnavailable
from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.input_models import InventoryInput

logger = logging.getLogger(__name__)


class DeviceInventoryLookupSkill(BaseSkill):
    input_model = InventoryInput
    def __init__(self, alarm_repo, device_repo) -> None:
        self.alarm_repo = alarm_repo
        self.device_repo = device_repo

    @property
    def name(self) -> str:
        return "device_inventory_lookup"

    @property
    def description(self) -> str:
        return (
            "Tra cứu thông tin thiết bị mạng trong inventory: tìm thiết bị theo tên, IP, "
            "hoặc device_id, trả về vendor, station, loại thiết bị, và các alarm đang active "
            "liên quan."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "Tên thiết bị (vd: NodeB-AG-Station-006-000001)",
                    "default": "",
                },
                "ip_address": {
                    "type": "string",
                    "description": "Địa chỉ IP (vd: 10.2.0.2)",
                    "default": "",
                },
                "device_id": {
                    "type": "string",
                    "description": "Device ID (vd: DEV-005859)",
                    "default": "",
                },
            },
        }

    async def execute(
        self,
        device_name: str = "",
        ip_address: str = "",
        device_id: str = "",
        **kwargs,
    ) -> SkillResult:
        if not device_name and not ip_address and not device_id:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error="Cần ít nhất 1 trong 3: device_name, ip_address, hoặc device_id",
            )

        try:
            # Step 1: Find device in PostgreSQL
            device = None
            if hasattr(self.device_repo, "find_device_candidates"):
                candidates = await self.device_repo.find_device_candidates(device_name=device_name, ip_address=ip_address, device_id=device_id)
                if len(candidates) > 1:
                    return SkillResult(status=SkillStatus.BUSINESS_ERROR, data={"candidates": candidates[:20], "truncated": len(candidates) > 20}, error="Ambiguous device identity; select an exact device_id")
                device = candidates[0] if candidates else None
            elif device_name:
                device = await self.device_repo.get_device_with_details(
                    device_name=device_name
                )
            elif ip_address:
                # Search by IP
                devices = await self.device_repo.get_devices_by_names([ip_address])
                if devices:
                    device = devices[0]
            elif device_id:
                device = await self.device_repo.get_device_with_details(
                    device_id=device_id
                )

            if not device:
                search_key = device_name or ip_address or device_id
                return SkillResult(
                    status=SkillStatus.BUSINESS_ERROR,
                    data=None,
                    error=f"Không tìm thấy thiết bị '{search_key}' trong inventory",
                )

            # Step 2: Get active alarms for this device
            try:
                active_alarms = await asyncio.to_thread(self.alarm_repo.get_active_alarms_by_device,
                    device["device_id"], limit=20
                )
            except ConnectorUnavailable as exc:
                # Inventory is still valid. Never report zero alarms for an
                # alarm source that could not be queried.
                return SkillResult(
                    status=SkillStatus.SUCCESS,
                    data={
                        "device": {
                            "device_id": device["device_id"],
                            "name": device["name"],
                            "device_type": device["device_type"],
                            "ip_address": device["ip_address"],
                            "vendor": device.get("vendor_name", "N/A"),
                            "station": device.get("station_name", "N/A"),
                        },
                        "alarm_source": "unavailable",
                        "active_alarms": None,
                        "active_alarm_count": None,
                        "warnings": [str(exc), "Alarm state is unknown, not zero."],
                    },
                )

            return SkillResult(
                status=SkillStatus.SUCCESS,
                data={
                    "device": {
                        "device_id": device["device_id"],
                        "name": device["name"],
                        "device_type": device["device_type"],
                        "ip_address": device["ip_address"],
                        "vendor": device.get("vendor_name", "N/A"),
                        "station": device.get("station_name", "N/A"),
                    },
                    "active_alarms": active_alarms,
                    "active_alarm_count": len(active_alarms),
                    "active_alarms_truncated": len(active_alarms) >= 20,
                    "active_alarm_count_is_lower_bound": len(active_alarms) >= 20,
                },
            )
        except Exception as e:
            logger.error("DeviceInventoryLookup error: %s", e)
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=str(e),
            )
