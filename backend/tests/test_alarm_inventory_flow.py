import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from core.schemas import AgentEventType, NormalizedResponse, NormalizedToolCall
from services.agent_service import AgentEngine
from services.skills.alarm_statistics import AlarmStatisticsSkill
from services.skills.device_inventory_lookup import DeviceInventoryLookupSkill
from services.skills.base import SkillStatus
from services.skills.registry import SkillRegistry
from services.tool_executor import ToolExecutor


class FakeAlarmRepo:
    def __init__(self):
        self.count_calls = []
        self.group_calls = []

    def count_alarms(self, where_clause, params):
        self.count_calls.append((where_clause, params))
        if "status = 'active'" in where_clause:
            return 3
        return 10

    def group_alarms(self, group_col, where_clause, params, limit=0):
        self.group_calls.append((group_col, where_clause, params, limit))
        if group_col == "severity":
            return [{"key": "Critical", "count": 2}, {"key": "Major", "count": 8}]
        if group_col == "error_code":
            return [{"key": "ERR-001", "count": 5}]
        if group_col == "device_id":
            return [{"key": "DEV-001", "count": 4}]
        return []

    def get_active_alarms_by_device(self, device_id, limit=20):
        return [
            {
                "alarm_id": "A-1",
                "error_code": "ERR-001",
                "severity": "Critical",
                "time_created": "2026-06-27 10:00:00",
                "status": "active",
            }
        ]


class FakeDeviceRepo:
    async def get_errors_by_domain(self, domain):
        if domain.lower() == "transport":
            return [{"error_code": "ERR-001"}]
        return []

    async def get_error_by_code(self, error_code):
        return {"error_code": error_code, "name": "Link Down"}

    async def get_device_with_details(self, device_id=None, device_name=None):
        if device_id == "DEV-001" or device_name == "NOC-APP-01":
            return {
                "device_id": "DEV-001",
                "name": "NOC-APP-01",
                "device_type": "Server",
                "ip_address": "10.0.0.1",
                "vendor_name": "Linux",
                "station_name": "NOC",
            }
        return None

    async def get_devices_by_names(self, names):
        if "10.0.0.1" in names or "NOC-APP-01" in names:
            return [
                {
                    "device_id": "DEV-001",
                    "name": "NOC-APP-01",
                    "device_type": "Server",
                    "ip_address": "10.0.0.1",
                    "vendor_name": "Linux",
                    "station_name": "NOC",
                }
            ]
        return []


def build_registry():
    alarm_repo = FakeAlarmRepo()
    device_repo = FakeDeviceRepo()
    registry = SkillRegistry()
    registry.register(AlarmStatisticsSkill(alarm_repo, device_repo))
    registry.register(DeviceInventoryLookupSkill(alarm_repo, device_repo))
    return registry, alarm_repo


class TwoSkillExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_alarm_statistics_execution_matches_skill_md_shape(self):
        registry, alarm_repo = build_registry()
        executor = ToolExecutor(registry)

        result = await executor.execute(
            "alarm_statistics",
            {
                "window_min": 60,
                "domain": "Transport",
                "severity": "Critical",
                "top_n": 3,
            },
        )

        self.assertEqual(result.status, SkillStatus.SUCCESS)
        self.assertEqual(result.data["total_alarms"], 10)
        self.assertEqual(result.data["group_by"], "severity")
        self.assertEqual(result.data["grouped_alarms"][0]["key"], "Critical")
        self.assertEqual(result.data["active_alarms"], 3)
        self.assertEqual(result.data["archived_alarms"], 7)
        self.assertEqual(result.data["by_severity"]["Critical"], 2)
        self.assertEqual(result.data["by_error_code"][0]["name"], "Link Down")
        self.assertEqual(result.data["top_devices"][0]["name"], "NOC-APP-01")
        self.assertIn("error_code IN %(domain_codes)s", alarm_repo.count_calls[0][0])
        self.assertEqual(alarm_repo.count_calls[0][1]["domain_codes"], ("ERR-001",))

    async def test_direct_device_inventory_lookup_execution_matches_skill_md_shape(self):
        registry, _ = build_registry()
        executor = ToolExecutor(registry)

        result = await executor.execute(
            "device_inventory_lookup",
            {"device_name": "NOC-APP-01"},
        )

        self.assertEqual(result.status, SkillStatus.SUCCESS)
        self.assertEqual(result.data["device"]["device_id"], "DEV-001")
        self.assertEqual(result.data["device"]["vendor"], "Linux")
        self.assertEqual(result.data["device"]["station"], "NOC")
        self.assertEqual(result.data["active_alarm_count"], 1)

    async def test_device_inventory_lookup_validates_required_identifier(self):
        registry, _ = build_registry()
        executor = ToolExecutor(registry)

        result = await executor.execute("device_inventory_lookup", {})

        self.assertEqual(result.status, SkillStatus.BUSINESS_ERROR)
        self.assertEqual(result.data["error_code"], "INVALID_TOOL_INPUT")

    async def test_agent_loop_executes_both_tools_when_provider_requests_them(self):
        registry, _ = build_registry()
        executor = ToolExecutor(registry)

        class FakeProvider:
            model_name = "fake"

            def __init__(self):
                self.call_count = 0

            async def chat_completion(self, messages, tools, stream=True):
                self.call_count += 1
                if self.call_count == 1:
                    tool_names = {tool.name for tool in tools}
                    assert "alarm_statistics" in tool_names
                    assert "device_inventory_lookup" in tool_names
                    yield NormalizedResponse(
                        thinking_content="Kiểm tra thống kê và inventory.\n",
                        tool_calls=[
                            NormalizedToolCall(
                                call_id="call_stats",
                                tool_name="alarm_statistics",
                                arguments={"window_min": 60, "top_n": 3},
                            ),
                            NormalizedToolCall(
                                call_id="call_inventory",
                                tool_name="device_inventory_lookup",
                                arguments={"device_name": "NOC-APP-01"},
                            ),
                        ],
                        stop_reason="tool_calls",
                    )
                    return

                tool_messages = [msg for msg in messages if msg.role == "tool"]
                assert len(tool_messages) == 2
                yield NormalizedResponse(
                    text_content="Đã tổng hợp thống kê alarm và inventory thiết bị.",
                    stop_reason="end_turn",
                )

        engine = AgentEngine(executor, registry, tracer=None)
        provider = FakeProvider()
        events = [
            event
            async for event in engine.run(
                session_id="session-1",
                user_message="Thống kê alarm và kiểm tra thiết bị NOC-APP-01",
                provider=provider,
                history=[],
            )
        ]

        starts = [event for event in events if event.type == AgentEventType.TOOL_CALL_START]
        results = [event for event in events if event.type == AgentEventType.TOOL_CALL_RESULT]
        final = next(event for event in events if event.type == AgentEventType.FINAL_ANSWER)

        self.assertEqual([event.data["tool_name"] for event in starts], [
            "alarm_statistics",
            "device_inventory_lookup",
        ])
        self.assertEqual([event.data["status"] for event in results], ["success", "success"])
        self.assertEqual(final.data["total_tool_calls"], 2)
        self.assertIn(
            "Đã tổng hợp",
            final.data["updated_messages"][-1].content,
        )


if __name__ == "__main__":
    unittest.main()
