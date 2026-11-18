import asyncio
import logging
from datetime import datetime, timedelta, timezone

from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.input_models import AlarmStatisticsInput

logger = logging.getLogger(__name__)

GROUP_BY_COLUMNS = {
    "severity": "severity",
    "error_code": "error_code",
    "device_id": "device_id",
}


class AlarmStatisticsSkill(BaseSkill):
    input_model = AlarmStatisticsInput
    def __init__(self, alarm_repo, device_repo) -> None:
        self.alarm_repo = alarm_repo
        self.device_repo = device_repo

    @property
    def name(self) -> str:
        return "alarm_statistics"

    @property
    def description(self) -> str:
        return (
            "Thống kê và phân tích xu hướng cảnh báo NOC: đếm tổng số alarm theo severity "
            "(Critical/Major/Warning/Minor), theo domain lỗi (RAN, Transport, Power, Core), "
            "theo thiết bị, hoặc theo khoảng thời gian. Trả về bảng tổng hợp, top thiết bị "
            "có nhiều alarm nhất, và tỷ lệ phần trăm phân bố."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "window_min": {
                    "type": "integer",
                    "description": "Cửa sổ thời gian (phút). Mặc định 1440 (24 giờ)",
                    "default": 1440,
                },
                "domain": {
                    "type": "string",
                    "description": "Filter theo domain lỗi: RAN, Transport, Power, Core. Để trống = tất cả",
                    "default": "",
                },
                "severity": {
                    "type": "string",
                    "description": "Filter theo severity: Critical, Major, Warning, Minor. Để trống = tất cả",
                    "default": "",
                },
                "group_by": {
                    "type": "string",
                    "description": "Cách nhóm: severity, error_code, device_id",
                    "default": "severity",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Số top items trả về",
                    "default": 10,
                },
            },
        }

    async def execute(
        self,
        window_min: int = 1440,
        domain: str = "",
        severity: str = "",
        group_by: str = "severity",
        top_n: int = 10,
        **kwargs,
    ) -> SkillResult:
        try:
            since = datetime.now(timezone.utc) - timedelta(minutes=window_min)

            # Build filter conditions
            conditions = ["time_created >= %(since)s"]
            params = {"since": since, "top_n": top_n}

            if severity:
                conditions.append("severity = %(severity)s")
                params["severity"] = severity

            group_by = (group_by or "severity").strip().lower()
            group_col = GROUP_BY_COLUMNS.get(group_by)
            if group_col is None:
                return SkillResult(
                    status=SkillStatus.BUSINESS_ERROR,
                    data=None,
                    error=(
                        "group_by không hợp lệ. Chỉ hỗ trợ: "
                        f"{', '.join(GROUP_BY_COLUMNS)}"
                    ),
                )

            where_clause = " AND ".join(conditions)

            # If domain filter, need to JOIN with error table
            if domain:
                # Get error_codes for this domain from PostgreSQL
                error_codes = await self._get_error_codes_by_domain(domain)
                if not error_codes:
                    return SkillResult(
                        status=SkillStatus.BUSINESS_ERROR,
                        data=None,
                        error=f"Không tìm thấy error code nào thuộc domain '{domain}'",
                    )
                params["domain_codes"] = tuple(error_codes)
                where_clause += " AND error_code IN %(domain_codes)s"

            # Query 1: Total count
            total = await asyncio.to_thread(self.alarm_repo.count_alarms, where_clause, params)

            # Query 2: By severity
            by_severity_raw = await asyncio.to_thread(self.alarm_repo.group_alarms,
                "severity", where_clause, params
            )
            by_severity = {item["key"]: item["count"] for item in by_severity_raw}

            # Query 3: By error_code (top N)
            by_error_code_raw = await asyncio.to_thread(self.alarm_repo.group_alarms,
                "error_code", where_clause, params, limit=top_n
            )

            # Enrich error_code with names from PostgreSQL
            by_error_code = []
            for item in by_error_code_raw:
                error_info = await self.device_repo.get_error_by_code(
                    item["key"]
                )
                by_error_code.append(
                    {
                        "error_code": item["key"],
                        "name": error_info["name"] if error_info else item["key"],
                        "count": item["count"],
                    }
                )

            # Query 4: Top devices
            top_devices_raw = await asyncio.to_thread(self.alarm_repo.group_alarms,
                "device_id", where_clause, params, limit=top_n
            )

            # Enrich device with names from PostgreSQL
            top_devices = []
            for item in top_devices_raw:
                device_info = await self.device_repo.get_device_with_details(
                    device_id=item["key"]
                )
                top_devices.append(
                    {
                        "device_id": item["key"],
                        "name": device_info["name"] if device_info else item["key"],
                        "count": item["count"],
                    }
                )

            # Query 5: Active vs archived
            active_count = await asyncio.to_thread(self.alarm_repo.count_alarms,
                where_clause + " AND status = 'active'", params
            )
            archived_count = total - active_count

            grouped_alarms = await asyncio.to_thread(self.alarm_repo.group_alarms,
                group_col, where_clause, params, limit=top_n
            )

            data = {
                "window_min": window_min,
                "group_by": group_by,
                "grouped_alarms": grouped_alarms,
                "total_alarms": total,
                "by_severity": by_severity,
                "by_error_code": by_error_code,
                "top_devices": top_devices,
                "active_alarms": active_count,
                "archived_alarms": archived_count,
                "domain_filter": domain or None,
                "severity_filter": severity or None,
            }
            if total == 0:
                data.update(await self._empty_window_context(since))
            return SkillResult(status=SkillStatus.SUCCESS, data=data)
        except Exception as e:
            logger.error("AlarmStatistics error: %s", e)
            return SkillResult(
                status=SkillStatus.SYSTEM_ERROR,
                data=None,
                error=str(e),
            )

    async def _empty_window_context(self, since):
        """An empty window is not "no alarms": say where the source's data actually ends.

        The newest time sits under a `time_created` key so the evidence layer treats it as
        the observation time and adds its stale-data warning on its own.
        """
        newest_of = getattr(self.alarm_repo, "newest_alarm_time", None)
        if newest_of is None:
            return {}
        try:
            newest = await asyncio.to_thread(newest_of)
        except Exception:
            return {"empty_window_note": (
                "Không có alarm trong cửa sổ, và không kiểm tra được bản ghi mới nhất của nguồn; "
                "không được kết luận là mạng không có alarm."
            )}
        if newest is None:
            return {"empty_window_note": "Nguồn alarm không có bản ghi nào."}
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        if newest >= since:
            return {}  # data exists in the window; filters (severity/domain) excluded it
        return {
            "source_latest_alarm": {"time_created": newest.isoformat()},
            "empty_window_note": (
                f"Không có alarm trong cửa sổ {round((datetime.now(timezone.utc) - since).total_seconds() / 60)} "
                f"phút, nhưng bản ghi mới nhất của toàn bộ nguồn là {newest.isoformat()}, cũ hơn cửa sổ. "
                "Nguồn có thể là dữ liệu tĩnh hoặc đã ngừng cập nhật; không được kết luận là mạng không có alarm. "
                "Hãy nêu thời điểm này cho người dùng."
            ),
        }

    async def _get_error_codes_by_domain(self, domain: str) -> list[str]:
        """Get error codes for a domain from PostgreSQL error table."""
        try:
            rows = await self.device_repo.get_errors_by_domain(domain)
            return [r["error_code"] for r in rows]
        except Exception:
            return []
