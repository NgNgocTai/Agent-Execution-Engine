import logging

from services.skills.base import BaseSkill, SkillResult, SkillStatus

logger = logging.getLogger(__name__)


class DeviceInventoryLookupSkill(BaseSkill):
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
            if device_name:
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
            active_alarms = self.alarm_repo.get_active_alarms_by_device(
                device["device_id"], limit=20
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
                },
            )
        except Exception as e:
            logger.error("DeviceInventoryLookup error: %s", e)
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=str(e),
            )
