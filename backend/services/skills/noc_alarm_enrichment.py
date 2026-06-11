import re
from datetime import datetime, timedelta

from data.mock_alarms import MOCK_ALARMS
from data.mock_inventory import MOCK_NE_INVENTORY
from services.skills.base import BaseSkill, SkillResult, SkillStatus


class NocAlarmEnrichmentSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "noc_alarm_enrichment"

    @property
    def description(self) -> str:
        return (
            "Điều tra và làm giàu cảnh báo NOC: lấy cảnh báo theo loại alarm_type "
            "trong cửa sổ thời gian window_min phút, trích xuất entity (IP, tên NE, "
            "interface, cell, AS) từ content, tra cứu inventory để lấy site_id, "
            "segment, vendor, oncall_team."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "alarm_type": {
                    "type": "string",
                    "description": "Loại cảnh báo cần điều tra (vd: LINK_DOWN, BGP_SESSION_DOWN)",
                },
                "window_min": {
                    "type": "integer",
                    "description": "Cửa sổ thời gian (phút). Mặc định 10.",
                    "default": 10,
                },
            },
            "required": ["alarm_type"],
        }

    async def execute(self, alarm_type: str, window_min: int = 10, **kwargs) -> SkillResult:
        alarms = MOCK_ALARMS.get(alarm_type, [])
        if not alarms:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error=f"Không tìm thấy cảnh báo loại '{alarm_type}'. Các loại có sẵn: {list(MOCK_ALARMS.keys())}",
            )

        enriched = []
        for alarm in alarms:
            extracted = self._extract_entities(alarm.get("content", ""))
            lookup_keys = extracted.get("ips", []) + extracted.get("ne_names", [])
            enrichment = self._lookup_inventory(lookup_keys)

            enriched.append({
                "alarm_id": alarm.get("alarm_id"),
                "alarm_type": alarm.get("alarm_type"),
                "ne_name": alarm.get("ne_name"),
                "content": alarm.get("content"),
                "severity": alarm.get("severity"),
                "event_time": alarm.get("event_time"),
                "extracted": extracted,
                "lookup_keys": lookup_keys,
                "enrichment": enrichment,
            })

        return SkillResult(
            status=SkillStatus.SUCCESS,
            data={"alarms": enriched, "total": len(enriched), "alarm_type": alarm_type, "window_min": window_min},
        )

    @staticmethod
    def _extract_entities(content: str) -> dict:
        ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", content)
        ne_names = re.findall(r"\bNE\s+([A-Z]{2,}[A-Z0-9-]+)", content)
        interfaces = re.findall(r"Interface\s+(\S+)", content)
        cell_ids = re.findall(r"\bcell\s+([A-Za-z0-9_]+)", content, re.IGNORECASE)
        as_numbers = re.findall(r"\bAS(\d+)\b", content)

        return {
            "ips": ips,
            "ne_names": ne_names,
            "interfaces": interfaces,
            "cell_ids": cell_ids,
            "as_numbers": [f"AS{n}" for n in as_numbers],
        }

    @staticmethod
    def _lookup_inventory(keys: list[str]) -> list[dict]:
        results = []
        for key in keys:
            entry = MOCK_NE_INVENTORY.get(key)
            if entry:
                results.append({"lookup_key": key, **entry})
        return results
