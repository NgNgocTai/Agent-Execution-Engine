import asyncio
import logging
import re

from services.skills.base import BaseSkill, SkillResult, SkillStatus
from services.skills.input_models import AlarmEnrichmentInput

logger = logging.getLogger(__name__)

# ============================================================
# Pattern mặc định để trích xuất entity từ raw_log/description.
#
# LƯU Ý QUAN TRỌNG: các pattern dưới đây được hiệu chỉnh theo dữ liệu THẬT
# lấy từ bảng `alarm` (ClickHouse) và `device` (PostgreSQL), KHÔNG theo ví dụ
# lý thuyết trong tài liệu thiết kế ban đầu. Định dạng raw_log thật là:
#
#   "[2026-06-01 00:00:20] Encoder-CT-Station-007-004385 Signal Loss
#    sev=Critical val=8 thr=68 st=ACK"
#
# Tên thiết bị KHÔNG có chữ "NE" đứng trước, nên ta giữ pattern cũ (yêu cầu
# có "NE") làm phương án dự phòng, đồng thời thêm pattern mới khớp đúng định
# dạng "{Type}-{Code}-Station-{N}-{N}" (khớp y nguyên device.name trong DB).
# ============================================================

DEFAULT_PATTERNS: dict[str, dict] = {
    "ips": {
        "regex": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        "group": 0,
    },
    "ne_names_with_prefix": {
        # Dạng cũ: có literal "NE" phía trước (vd: "NE HNI-CORE-01")
        "regex": r"\bNE\s+([A-Z]{2,}[A-Z0-9-]+)",
        "group": 1,
    },
    "ne_names_station": {
        # Dạng thật trong production: "{Type}-{Code}-Station-{N}-{N}",
        # khớp đúng device.name (vd: "Encoder-CT-Station-007-004385")
        "regex": r"\b([A-Za-z]+-[A-Za-z]+-Station-\d+-\d+)\b",
        "group": 1,
    },
    "interfaces": {
        "regex": r"Interface\s+(\S+)",
        "group": 1,
    },
    "cell_ids": {
        "regex": r"\bcell\s+([A-Za-z0-9_]+)",
        "group": 1,
        "flags": re.IGNORECASE,
    },
    "as_numbers": {
        "regex": r"\bAS(\d+)\b",
        "group": 1,
    },
    "vlans": {
        # Chưa thấy xuất hiện trong data mẫu thật, nhưng giữ theo yêu cầu
        # đặc tả (SKILL.md) để sẵn sàng nếu log dạng khác có VLAN.
        "regex": r"\bVLAN[\s:=#-]*(\d+)\b",
        "group": 1,
        "flags": re.IGNORECASE,
    },
    "kv": {
        # Bắt cặp key=value xuất hiện rất phổ biến trong log thật
        # (vd: sev=Critical val=8 thr=68 st=ACK)
        "regex": r"\b([a-zA-Z_][a-zA-Z0-9_]*)=([A-Za-z0-9.\-]+)",
        "group": "kv_pair",  # xử lý đặc biệt, xem _extract_entities
    },
}


class NocAlarmEnrichmentSkill(BaseSkill):
    input_model = AlarmEnrichmentInput
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
            "interface, cell, AS, VLAN, key=value) từ content, tra cứu inventory để lấy "
            "vendor, station_name, device_type. Dùng regex built-in đã review; custom regex "
            "cần một skill version được review trong sandbox."
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
                "custom_patterns": {
                    "type": "object",
                    "description": (
                        "Tuỳ chọn: dict các pattern bổ sung/ghi đè để trích xuất entity, "
                        "KHÔNG cần sửa code. Format: "
                        '{"ten_field": {"regex": "...", "group": 0 hoặc 1}}. '
                        "group=0 lấy toàn bộ khớp, group=1 lấy nhóm bắt đầu tiên. "
                        "Field trùng tên với pattern mặc định sẽ bị ghi đè."
                    ),
                    "default": {},
                },
            },
            "required": ["alarm_type"],
        }

    async def execute(
        self,
        alarm_type: str = "",
        window_min: int = 10,
        custom_patterns: dict | None = None,
        **kwargs,
    ) -> SkillResult:
        if not alarm_type or not alarm_type.strip():
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error="Thiếu tham số bắt buộc: alarm_type. Vui lòng cung cấp loại cảnh báo (vd: LINK_DOWN, BGP_SESSION_DOWN).",
            )

        alarm_type = alarm_type.strip().upper().replace("_", " ")

        error_info = await self.device_repo.get_error_by_name(alarm_type)
        if not error_info:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error=f"Không tìm thấy loại lỗi '{alarm_type}' trong danh mục.",
            )

        error_code = error_info["error_code"]

        alarms, total_alarms = await asyncio.to_thread(self.alarm_repo.get_alarms, error_code, window_min, limit=50)
        if not alarms:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error=f"Không có cảnh báo '{alarm_type}' trong {window_min} phút qua.",
            )

        # Gộp pattern mặc định + custom_patterns (custom đè lên mặc định nếu trùng tên)
        active_patterns = dict(DEFAULT_PATTERNS)
        if custom_patterns:
            for field_name, spec in custom_patterns.items():
                if not isinstance(spec, dict) or "regex" not in spec:
                    logger.warning("custom_patterns['%s'] thiếu 'regex', bỏ qua.", field_name)
                    continue
                active_patterns[field_name] = spec

        unmatched_count = 0
        enriched = []
        for alarm in alarms:
            # Ghép cả raw_log VÀ description (không dùng "or" chọn 1 trong 2),
            # vì description thường chứa ghi chú xử lý có giá trị cho LLM
            # (vd: "Phoi hop voi vendor de xu ly", "Da cap nhat firmware")
            # mà raw_log không có. filter(None, ...) tự loại field rỗng/None
            # nên không tạo khoảng trắng dư khi 1 trong 2 trống.
            content = " ".join(filter(None, [alarm.get("raw_log"), alarm.get("description")]))
            extracted = self._extract_entities(content, active_patterns)

            # Lookup key CỐ ĐỊNH là ips + ne_names (gộp cả 2 dạng ne_names).
            # KHÔNG cho phép tuỳ chỉnh field nào dùng để lookup, vì bảng
            # device trong PostgreSQL chỉ index theo `name` và `ip_address`
            # (xem postgres_repo.py: WHERE d.name = ANY($1) OR d.ip_address = ANY($1)).
            # Thêm field khác (interfaces, vlans, kv...) vào lookup sẽ luôn
            # trả về rỗng vì DB không có cột tương ứng để so khớp.
            ne_name_candidates = (
                extracted.get("ne_names_with_prefix", [])
                + extracted.get("ne_names_station", [])
            )
            lookup_keys = list(set(extracted.get("ips", []) + ne_name_candidates))

            enrichment = []
            if lookup_keys and self.device_repo:
                devices = await self.device_repo.get_devices_by_names(lookup_keys)
                for d in devices:
                    enrichment.append({
                        "device_id": d["device_id"],
                        "device_name": d["name"],
                        "ip_address": d["ip_address"],
                        "device_type": d["device_type"],
                        "vendor": d["vendor_name"],
                        "station": d["station_name"],
                        # oncall_team / site_id / segment: CHƯA CÓ trong schema
                        # hiện tại (đã kiểm tra alarm_data.device/station/vendor/
                        # error — không có cột nào liên quan đội trực). Đây là
                        # tính năng mở rộng tương lai, cần bổ sung bảng/cột riêng
                        # khi có nguồn dữ liệu thật về phân công ca trực.
                    })

            if lookup_keys and not enrichment:
                unmatched_count += 1

            enriched.append({
                "alarm_id": alarm["alarm_id"],
                "error_name": alarm_type,
                "device_id": alarm["device_id"],
                "raw_log": alarm.get("raw_log"),
                "description": alarm.get("description"),
                "content": content,  # raw_log + description đã ghép, dùng để extract entity
                "severity": alarm["severity"],
                "status": alarm["status"],
                "time_created": alarm["time_created"],
                "time_solved": alarm.get("time_solved"),
                "extracted": extracted,
                "enrichment": enrichment,
                "inventory_match_status": "unmatched" if not enrichment else "candidate" if len(enrichment) == 1 else "multiple_candidates",
            })

        note_parts = []
        if total_alarms > len(enriched):
            note_parts.append(
                f"Vì số lượng cảnh báo quá lớn ({total_alarms}), hệ thống chỉ hiển thị "
                f"chi tiết {len(enriched)} cảnh báo mới nhất để tránh quá tải bộ nhớ."
            )
        if unmatched_count:
            note_parts.append(
                f"{unmatched_count} cảnh báo trích xuất được entity nhưng KHÔNG khớp "
                f"thiết bị nào trong inventory — có thể do định dạng tên/IP trong log "
                f"khác với device.name/ip_address, hoặc thiết bị chưa được khai báo."
            )

        return SkillResult(
            status=SkillStatus.SUCCESS,
            data={
                "alarms": enriched,
                "total_alarms_in_db": total_alarms,
                "displayed_alarms": len(enriched),
                "truncated": total_alarms > len(enriched),
                "alarm_type": alarm_type,
                "error_info": error_info,
                "window_min": window_min,
                "note": " ".join(note_parts) if note_parts else None,
            },
        )

    @staticmethod
    def _extract_entities(content: str, patterns: dict[str, dict]) -> dict:
        result: dict[str, list] = {}

        for field_name, spec in patterns.items():
            regex = spec["regex"]
            flags = spec.get("flags", 0)
            group = spec.get("group", 0)

            try:
                compiled = re.compile(regex, flags)
            except re.error as e:
                logger.warning("Pattern lỗi cho field '%s': %s", field_name, e)
                result[field_name] = []
                continue

            if group == "kv_pair":
                # Trường hợp đặc biệt: trả về dict key=value thay vì list
                pairs = compiled.findall(content)
                result[field_name] = {k: v for k, v in pairs}
                continue

            if group == 0:
                matches = compiled.findall(content)
                # findall với group=0 trả về toàn bộ chuỗi khớp (string),
                # không phải tuple, nên giữ nguyên.
                result[field_name] = list(set(matches))
            else:
                raw_matches = compiled.findall(content)
                # Nếu regex có nhiều group, findall trả tuple; ta lấy đúng group chỉ định.
                cleaned = []
                for m in raw_matches:
                    if isinstance(m, tuple):
                        idx = group - 1 if group >= 1 else 0
                        cleaned.append(m[idx] if idx < len(m) else m[0])
                    else:
                        cleaned.append(m)
                result[field_name] = list(set(cleaned))

        # Hậu xử lý field as_numbers để giữ format "ASxxxx" như cũ
        if "as_numbers" in result:
            result["as_numbers"] = [f"AS{n}" for n in result["as_numbers"]]

        return result
