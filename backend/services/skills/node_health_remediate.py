import random

from data.mock_inventory import MOCK_NE_INVENTORY
from services.skills.base import BaseSkill, SkillResult, SkillStatus


MOCK_METRICS: dict[str, dict] = {
    "10.211.140.16": {"mem_pct": 91.2, "cpu_pct": 88.0, "load1": 12.5, "disk_pct": 65.0},
    "10.211.140.17": {"mem_pct": 45.0, "cpu_pct": 30.0, "load1": 2.1, "disk_pct": 40.0},
    "10.211.140.1":  {"mem_pct": 78.0, "cpu_pct": 82.0, "load1": 8.3, "disk_pct": 55.0},
}


class NodeHealthRemediateSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "node_health_remediate"

    @property
    def description(self) -> str:
        return (
            "SSH vào node mạng, đo RAM% và CPU%, rồi tự động xử lý theo ngưỡng: "
            "RAM>ngưỡng X VÀ CPU>ngưỡng Y → restart docker; "
            "RAM>ngưỡng X nhưng CPU<=Y → đọc log container; "
            "RAM<=ngưỡng X nhưng CPU>Y → restart container; "
            "cả hai dưới ngưỡng → không làm gì."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "IP hoặc hostname của node (bắt buộc)",
                },
                "ram_threshold": {
                    "type": "integer",
                    "description": "Ngưỡng RAM% để coi là cao (bắt buộc)",
                },
                "cpu_threshold": {
                    "type": "integer",
                    "description": "Ngưỡng CPU% để coi là cao (bắt buộc)",
                },
                "service": {
                    "type": "string",
                    "description": "Tên container docker (cần cho nhánh đọc log & restart service)",
                    "default": "",
                },
                "user": {
                    "type": "string",
                    "description": "User SSH",
                    "default": "noc",
                },
                "password": {
                    "type": "string",
                    "description": "Mật khẩu SSH (nên dùng env SSH_PASSWORD)",
                    "default": "",
                },
            },
            "required": ["host", "ram_threshold", "cpu_threshold"],
        }

    async def execute(
        self,
        host: str,
        ram_threshold: int,
        cpu_threshold: int,
        service: str = "",
        user: str = "noc",
        password: str = "",
        **kwargs,
    ) -> SkillResult:
        inventory = MOCK_NE_INVENTORY.get(host)
        if not inventory:
            return SkillResult(
                status=SkillStatus.BUSINESS_ERROR,
                data=None,
                error=f"Node '{host}' không tồn tại trong inventory. Các node có sẵn: {list(MOCK_NE_INVENTORY.keys())}",
            )

        metrics = MOCK_METRICS.get(host)
        if not metrics:
            metrics = {
                "mem_pct": round(random.uniform(20, 95), 1),
                "cpu_pct": round(random.uniform(10, 90), 1),
                "load1": round(random.uniform(0.5, 15), 1),
                "disk_pct": round(random.uniform(30, 80), 1),
            }

        ram_high = metrics["mem_pct"] > ram_threshold
        cpu_high = metrics["cpu_pct"] > cpu_threshold

        if ram_high and cpu_high:
            action = "restart_docker"
            command = "sudo -n systemctl restart docker"
        elif ram_high and not cpu_high:
            action = "read_logs"
            command = f"docker logs --timestamps --tail 200 {service}" if service else None
        elif not ram_high and cpu_high:
            action = "restart_service"
            command = f"docker restart {service}" if service else None
        else:
            action = "none"
            command = None

        evaluation = {
            "ram_high": ram_high,
            "cpu_high": cpu_high,
            "ram_threshold": ram_threshold,
            "cpu_threshold": cpu_threshold,
        }

        result_data = {
            "host": host,
            "inventory": inventory,
            "metrics": metrics,
            "evaluation": evaluation,
            "action": action,
            "command": command,
            "executed": False,
        }

        return SkillResult(status=SkillStatus.SUCCESS, data=result_data)
