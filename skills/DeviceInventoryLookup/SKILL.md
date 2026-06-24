---
name: device_inventory_lookup
description: >-
  Tra cứu thông tin thiết bị mạng trong inventory: tìm thiết bị theo tên, IP, hoặc device_id,
  trả về vendor, station, loại thiết bị, và các alarm đang active liên quan.
  Dùng skill này BẤT CỨ KHI NÀO người dùng muốn xem thông tin thiết bị, "thiết bị này của hãng nào",
  "thuộc trạm nào", tra cứu inventory, hoặc kiểm tra trạng thái thiết bị.
---

# Device Inventory Lookup

Tra cứu thông tin thiết bị từ PostgreSQL + kiểm tra alarm active từ ClickHouse.

## Tham số

| Tham số | Kiểu | Bắt buộc | Mô tả |
|---------|------|----------|-------|
| `device_name` | string | Không | Tên thiết bị |
| `ip_address` | string | Không | Địa chỉ IP |
| `device_id` | string | Không | Device ID |

Ít nhất 1 trong 3 tham số phải được cung cấp.

## Output

```python
SkillResult(data={
    "device": {"device_id", "name", "device_type", "ip_address", "vendor", "station"},
    "active_alarms": [{"alarm_id", "error_code", "severity", "time_created"}],
    "active_alarm_count": 3,
})
```
