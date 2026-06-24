---
name: alarm_statistics
description: >-
  Thống kê và phân tích xu hướng cảnh báo NOC: đếm tổng số alarm theo severity (Critical/Major/Warning/Minor),
  theo domain lỗi (RAN, Transport, Power, Core), theo thiết bị, hoặc theo khoảng thời gian.
  Trả về bảng tổng hợp, top thiết bị có nhiều alarm nhất, và tỷ lệ phần trăm phân bố.
  Dùng skill này BẤT CỨ KHI NÀO người dùng muốn xem thống kê cảnh báo, "bao nhiêu alarm hôm nay",
  "alarm nào nhiều nhất", phân tích xu hướng, dashboard overview, hoặc tổng hợp tình hình mạng.
---

# Alarm Statistics

Thống kê cảnh báo NOC từ ClickHouse: **đếm → phân loại → xếp hạng → trả bảng tổng hợp**.

## Tham số

| Tham số | Kiểu | Mặc định | Mô tả |
|---------|------|----------|-------|
| `window_min` | integer | 1440 | Cửa sổ thời gian (phút). 1440 = 24 giờ |
| `domain` | string | "" | Filter: RAN, Transport, Power, Core |
| `severity` | string | "" | Filter: Critical, Major, Warning, Minor |
| `group_by` | string | "severity" | Nhóm: severity, error_code, device_id |
| `top_n` | integer | 10 | Số top items trả về |

## Output

```python
SkillResult(data={
    "window_min": 1440,
    "total_alarms": 12345,
    "by_severity": {"Critical": 150, "Major": 400, ...},
    "by_error_code": [{"error_code": "BRO-0045", "name": "Signal Loss", "count": 500}],
    "top_devices": [{"device_id": "DEV-005859", "name": "...", "count": 50}],
    "active_alarms": 200,
})
```
