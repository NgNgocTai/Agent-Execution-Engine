import logging
import re

from services.skills.base import BaseSkill, SkillResult, SkillStatus

logger = logging.getLogger(__name__)


class NocAlarmEnrichmentSkill(BaseSkill):
    def __init__(self, alarm_repo=None, device_repo=None) -> None:
        self.alarm_repo = alarm_repo
        self.device_repo = device_repo

    @property
    def name(self) -> str:
        return "noc_alarm_enrichment"

    @property
    def description(self) -> str:
        return (
            "Điều tra và làm giàu cảnh báo NOC: lấy cảnh báo theo loại alarm_type "
            "trong cửa sổ thời gian window_min phút, trích xuất entity (IP, tên NE, "
            "interface, cell, AS) từ content, tra cứu inventory để lấy vendor, "
            "station_name, device_type."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "alarm_type": {
                    "type": "string",
                    "description": "Loại cảnh báo cần điều tra (vd: LINK_DOWN, BGP_SESSION_DOWN, HIGH_CPU)",
                },
                "window_min": {
                    "type": "integer",
                    "description": "Cửa sổ thời gian (phút). Mặc định 10.",
                    "default": 10,
                },
            },
            "required": ["alarm_type"],
        }

    async def execute(self, alarm_type: str = "", window_min: int = 10, **kwargs) -> SkillResult:
        if not alarm_type or not alarm_type.strip():
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error="Thiếu tham số bắt buộc: alarm_type. Vui lòng cung cấp loại cảnh báo (vd: LINK_DOWN, BGP_SESSION_DOWN).",
            )

        alarm_type = alarm_type.strip().upper()

        error_info = await self.device_repo.get_error_by_name(alarm_type)
        if not error_info:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error=f"Không tìm thấy loại lỗi '{alarm_type}' trong danh mục.",
            )

        error_code = error_info["error_code"]

        alarms, total_alarms = self.alarm_repo.get_alarms(error_code, window_min, limit=50)
        if not alarms:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error=f"Không có cảnh báo '{alarm_type}' trong {window_min} phút qua.",
            )

        enriched = []
        for alarm in alarms:
            content = alarm.get("raw_log", "") or alarm.get("description", "")
            extracted = self._extract_entities(content)

            lookup_keys = list(set(extracted.get("ips", []) + extracted.get("ne_names", [])))
            enrichment = []
            if lookup_keys and self.device_repo:
                devices = await self.device_repo.get_devices_by_names(lookup_keys)
                for d in devices:
                    enrichment.append({
                        "device_name": d["name"],
                        "ip_address": d["ip_address"],
                        "device_type": d["device_type"],
                        "vendor": d["vendor_name"],
                        "station": d["station_name"],
                    })

            enriched.append({
                "alarm_id": alarm["alarm_id"],
                "error_name": alarm_type,
                "device_id": alarm["device_id"],
                "raw_log": content,
                "severity": alarm["severity"],
                "status": alarm["status"],
                "time_created": alarm["time_created"],
                "time_solved": alarm.get("time_solved"),
                "extracted": extracted,
                "enrichment": enrichment,
            })

        return SkillResult(
            status=SkillStatus.SUCCESS,
            data={
                "alarms": enriched,
                "total_alarms_in_db": total_alarms,
                "displayed_alarms": len(enriched),
                "alarm_type": alarm_type,
                "error_info": error_info,
                "window_min": window_min,
                "note": f"Vì số lượng cảnh báo quá lớn ({total_alarms}), hệ thống chỉ hiển thị chi tiết {len(enriched)} cảnh báo mới nhất để tránh quá tải bộ nhớ." if total_alarms > len(enriched) else ""
            },
        )

    @staticmethod
    def _extract_entities(content: str) -> dict:
        ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", content)
        ne_names = re.findall(r"\bNE\s+([A-Z]{2,}[A-Z0-9-]+)", content)
        interfaces = re.findall(r"Interface\s+(\S+)", content)
        cell_ids = re.findall(r"\bcell\s+([A-Za-z0-9_]+)", content, re.IGNORECASE)
        as_numbers = re.findall(r"\bAS(\d+)\b", content)

        return {
            "ips": list(set(ips)),
            "ne_names": list(set(ne_names)),
            "interfaces": list(set(interfaces)),
            "cell_ids": list(set(cell_ids)),
            "as_numbers": [f"AS{n}" for n in as_numbers],
        }
